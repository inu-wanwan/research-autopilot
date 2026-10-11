"""Static safety scan of untrusted source (pattern based; it cannot prove safety).

``critical`` findings fail verification (the repository is never registered); ``warning`` findings are shown
to the human in the execution approval request.  The scan reads files as text only.
"""

from __future__ import annotations

import re
from pathlib import Path

from research_autopilot.external.models import SafetyFinding

TEXT_EXT = {".py", ".sh", ".bash", ".zsh", ".m", ".r", ".jl", ".js", ".ts", ".c", ".cc", ".cpp", ".h", ".hpp",
            ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json", ".txt", ".md", ".rst", ".ipynb", ".mk", ""}
BINARY_EXT = {".so", ".dylib", ".dll", ".exe", ".bin", ".pyc", ".pyd", ".whl", ".jar", ".class", ".o", ".a"}
MAX_FILE = 2_000_000

RULES: list[tuple[str, str, re.Pattern]] = [
    ("critical", "credential_access", re.compile(
        r"(\.ssh/(id_|authorized_keys|known_hosts)|\.aws/credentials|\.netrc\b|\.git-credentials|"
        r"\.docker/config\.json|/etc/shadow|\.config/gcloud|Library/Keychains|\.kube/config)")),
    ("critical", "crypto_miner", re.compile(r"(stratum\+tcp://|xmrig|coinhive|cryptonight)", re.I)),
    ("critical", "pipe_to_shell", re.compile(r"(curl|wget)\s[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b")),
    ("critical", "obfuscated_exec", re.compile(
        r"(exec|eval)\s*\(\s*(base64\.b64decode|codecs\.decode|zlib\.decompress|marshal\.loads|bytes\.fromhex)")),
    ("critical", "reverse_shell", re.compile(r"(/dev/tcp/\d|socket\.socket\([^)]*\)[^\n]*\n[^\n]*os\.dup2|nc\s+-e\s)")),
    ("warning", "subprocess", re.compile(r"\b(subprocess\.(run|call|Popen|check_output|check_call)|os\.system|os\.popen)\b")),
    ("warning", "network_access", re.compile(r"\b(requests\.(get|post|put)|urllib\.request|urlopen|http\.client|"
                                             r"socket\.socket|paramiko|ftplib|smtplib)\b")),
    ("warning", "dynamic_code", re.compile(r"(?<![\w.])(eval|exec)\s*\(")),
    ("warning", "unsafe_deserialization", re.compile(r"\b(pickle|cPickle|dill|joblib)\.loads?\s*\(")),
    ("warning", "environment_read", re.compile(r"os\.environ\b(?!\.setdefault\(['\"](MPLBACKEND|OMP_NUM_THREADS))")),
    ("warning", "home_access", re.compile(r"(expanduser\(\s*['\"]~|Path\.home\(\))")),
    ("warning", "large_encoded_blob", re.compile(r"['\"][A-Za-z0-9+/=]{2000,}['\"]")),
]


def scan(root: Path, max_findings_per_rule: int = 20) -> list[SafetyFinding]:
    out: list[SafetyFinding] = []
    counts: dict[str, int] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() in BINARY_EXT:
            out.append(SafetyFinding(severity="warning", rule="binary_file", path=rel,
                                     excerpt="prebuilt binary / bytecode shipped in the repository"))
            continue
        if p.suffix.lower() not in TEXT_EXT or p.stat().st_size > MAX_FILE:
            continue
        text = p.read_text(errors="replace")
        for severity, rule, pat in RULES:
            for m in pat.finditer(text):
                if counts.get(rule, 0) >= max_findings_per_rule:
                    break
                counts[rule] = counts.get(rule, 0) + 1
                line = text.count("\n", 0, m.start()) + 1
                excerpt = text.splitlines()[line - 1].strip()[:160] if text else ""
                out.append(SafetyFinding(severity=severity, rule=rule, path=rel, line=line, excerpt=excerpt))
        if p.name == "setup.py" and re.search(r"cmdclass|urlopen|requests\.|subprocess", text):
            out.append(SafetyFinding(severity="warning", rule="custom_setup", path=rel,
                                     excerpt="setup.py customizes installation (never executed by RA)"))
    return out
