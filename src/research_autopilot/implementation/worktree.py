"""Per-task Git branch / worktree management with an exclusive edit lock.

Safety rules (docs/phase_specs/phase3.md §7):
- never checks out, commits to, merges into or pushes any branch other than
  the task branch ``autopilot/<task>-<run8>``; ``main``/``master`` untouched;
- never discards uncommitted changes (no reset / checkout -- / clean);
- every git invocation is appended to the audit callback.
"""

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from research_autopilot.implementation.models import GitAuditEntry, RepoConfig

PROTECTED_BRANCHES = {"main", "master"}

# Generated files that are never part of an implementation (excluded from scope checks, diffs and commits).
NOISE_PATTERNS = ["**/__pycache__/**", "**/*.pyc", ".pytest_cache/**", ".mypy_cache/**", ".ruff_cache/**",
                  "**/.DS_Store"]
_NOISE_PATHSPEC = [f":(exclude,glob){p}" for p in NOISE_PATTERNS]


class GitError(Exception):
    pass


class LockHeld(Exception):
    """Another backend (or process) holds the worktree edit lock."""


def glob_to_regex(pattern: str) -> re.Pattern:
    """Glob with ``**`` (any depth), ``*`` (one path segment) and ``?``."""
    p = pattern.strip().removeprefix("./")
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif p.startswith("**", i):
            out += ".*"; i += 2
        elif p[i] == "*":
            out += "[^/]*"; i += 1
        elif p[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(p[i]); i += 1
    return re.compile(f"^{out}$")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns if p.strip())


def parse_scope(text: str | None) -> list[str]:
    """Allowed-files section -> list of globs (one per bullet / line; backticks stripped)."""
    globs = []
    for line in (text or "").splitlines():
        item = re.sub(r"^\s*(?:[-*・]|\d+[.)])\s*", "", line).strip().strip("`").strip()
        item = item.split("  #")[0].split(" — ")[0].strip()
        if item and not item.startswith("#"):
            globs.append(item)
    return globs


class WorktreeManager:
    def __init__(self, repo: RepoConfig, worktrees_dir: Path,
                 audit: Callable[[GitAuditEntry], None] | None = None) -> None:
        self.repo = repo
        self.repo_path = Path(repo.path).expanduser().resolve()
        self.worktrees_dir = worktrees_dir.expanduser().resolve()
        self.locks_dir = self.worktrees_dir / ".locks"
        self._audit = audit or (lambda e: None)

    # ---------------------------------------------------------------- git

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        cwd = cwd or self.repo_path
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        self._audit(GitAuditEntry(args=list(args), cwd=str(cwd), exit_code=p.returncode))
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()[:300]}")
        return p.stdout

    def _add(self, path: Path, *flags: str) -> None:
        """``git add -A`` without noise paths. Git exits 1 when an excluded noise path (e.g. .ruff_cache) is also
        gitignored, although everything else was added; only that case is tolerated."""
        args = ("add", "-A", *flags, "--", ".", *_NOISE_PATHSPEC)
        p = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        self._audit(GitAuditEntry(args=list(args), cwd=str(path), exit_code=p.returncode))
        err = p.stderr.strip()
        if p.returncode != 0 and not (p.returncode == 1 and "ignored by one of your .gitignore files" in err
                                      and "fatal" not in err.lower()):
            raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {err[:300]}")

    # ---------------------------------------------------------------- repository checks

    def verify_repository(self) -> dict:
        """Existence, git-ness, base branch / commit, write access. Read-only."""
        if not self.repo_path.is_dir():
            raise GitError(f"repository path does not exist: {self.repo_path}")
        if self.git("rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
            raise GitError(f"not a git repository: {self.repo_path}")
        base_commit = self.git("rev-parse", "--verify", f"{self.repo.base_branch}^{{commit}}").strip()
        if not os.access(self.repo_path / ".git", os.W_OK):
            raise GitError("no write access to the repository's .git directory")
        dirty = bool(self.git("status", "--porcelain").strip())
        return {"base_commit": base_commit, "main_checkout_dirty": dirty,
                "head_branch": self.git("rev-parse", "--abbrev-ref", "HEAD").strip()}

    # ---------------------------------------------------------------- worktree lifecycle

    def ensure_worktree(self, branch: str, path: Path, base_commit: str) -> str:
        """Create (or re-attach to) the task worktree. Returns its HEAD commit.

        Idempotent: if the branch / worktree already exist (recovery) they are
        reused as-is, keeping any uncommitted work."""
        if branch.split("/")[-1] in PROTECTED_BRANCHES or branch in PROTECTED_BRANCHES:
            raise GitError(f"refusing to use protected branch {branch}")
        path = path.expanduser().resolve()
        if path.exists() and (path / ".git").exists():
            current = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()
            if current != branch:
                raise GitError(f"existing worktree {path} is on {current}, expected {branch}")
            return self.head(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip()
        if exists:
            self.git("worktree", "add", str(path), branch)
        else:
            self.git("worktree", "add", "-b", branch, str(path), base_commit)
        return self.head(path)

    def head(self, path: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=path).strip()

    # ---------------------------------------------------------------- lock

    def acquire_lock(self, path: Path, owner: dict) -> Path:
        """Exclusive edit lock for one worktree. A stale lock (dead pid on this
        host) is taken over; a live one raises LockHeld."""
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        lock = self.locks_dir / (Path(path).name + ".lock")
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                info = self.read_lock(lock)
                if info and _pid_alive(info.get("pid")) and info.get("host") == os.uname().nodename:
                    raise LockHeld(f"worktree locked by {info}") from None
                lock.unlink(missing_ok=True)  # stale
                continue
            with os.fdopen(fd, "w") as f:
                json.dump({**owner, "pid": owner.get("pid", os.getpid()), "host": os.uname().nodename}, f)
            return lock
        raise LockHeld(f"could not acquire {lock}")

    @staticmethod
    def read_lock(lock: Path) -> dict | None:
        try:
            return json.loads(lock.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def release_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)

    # ---------------------------------------------------------------- diff / commit

    def changed_files(self, path: Path, base_commit: str) -> list[str]:
        """Files differing from base: committed on the branch + staged + unstaged + untracked."""
        files = set(self.git("diff", "--name-only", base_commit, cwd=path).split())
        files |= set(self.git("ls-files", "--others", "--exclude-standard", cwd=path).split())
        return sorted(f for f in files if f and not matches_any(f, NOISE_PATTERNS))

    # ---------------------------------------------------------------- reading a fixed commit

    def object_type(self, commit: str, rel: str) -> str | None:
        out = self.git("cat-file", "-t", f"{commit}:{rel}", check=False).strip()
        return out or None

    def file_at(self, commit: str, rel: str) -> bool:
        return self.object_type(commit, rel.removeprefix("./")) == "blob"

    def dir_at(self, commit: str, rel: str) -> bool:
        return self.object_type(commit, rel.removeprefix("./").rstrip("/")) == "tree"

    def read_at(self, commit: str, rel: str) -> str | None:
        if not self.file_at(commit, rel):
            return None
        return self.git("show", f"{commit}:{rel}")

    def changed_between(self, a: str, b: str) -> list[str]:
        return sorted(f for f in self.git("diff", "--name-only", a, b).split() if f)

    def has_uncommitted(self, path: Path) -> bool:
        return bool(self.git("status", "--porcelain", "--", ".", *_NOISE_PATHSPEC, cwd=path).strip())

    def diff(self, path: Path, base_commit: str) -> str:
        self._add(path, "--intent-to-add")  # include untracked files
        return self.git("diff", base_commit, "--", ".", *_NOISE_PATHSPEC, cwd=path)

    def diff_stat(self, path: Path, base_commit: str) -> str:
        self._add(path, "--intent-to-add")
        return self.git("diff", "--stat", base_commit, "--", ".", *_NOISE_PATHSPEC, cwd=path)

    def commit_all(self, path: Path, message: str) -> str | None:
        """Commit everything on the task branch. Returns the new commit or None if nothing changed."""
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip()
        if branch in PROTECTED_BRANCHES:
            raise GitError(f"refusing to commit on protected branch {branch}")
        self._add(path)
        if not self.git("diff", "--cached", "--name-only", cwd=path).strip():
            return None
        self.git("-c", "user.name=Research Autopilot", "-c", "user.email=research-autopilot@localhost",
                 "commit", "--no-verify", "-m", message, cwd=path)
        return self.head(path)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
