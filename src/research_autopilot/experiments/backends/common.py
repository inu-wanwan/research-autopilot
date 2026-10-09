"""Artifact collection shared by backends."""

import hashlib
import os
import shutil
from pathlib import Path

from research_autopilot.experiments.models import ArtifactRecord, CollectResult


def collect_outputs(job_key: str, run_dir: Path, workdir: Path, outputs: list[str]) -> CollectResult:
    """Copy declared outputs into ``run_dir/artifacts`` (read-only) with sha256; never overwrite."""
    dest_root = run_dir / "artifacts"
    records, missing = [], []
    for rel in outputs:
        src = (workdir / rel).resolve()
        if not str(src).startswith(str(workdir.resolve())) or not src.is_file():
            missing.append(rel)
            continue
        dest = dest_root / rel
        if dest.exists():  # collected already (idempotent)
            data = dest.read_bytes()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            os.chmod(dest, 0o444)
            data = dest.read_bytes()
        records.append(ArtifactRecord(path=rel, stored_path=str(dest), sha256=hashlib.sha256(data).hexdigest(),
                                      size_bytes=len(data)))
    return CollectResult(job_key=job_key, artifacts=records, missing=missing,
                         stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"))
