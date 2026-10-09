"""JSON artifacts on disk (worker named volume in production)."""

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def write_json(path: Path, model: BaseModel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(model.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def run_record_path(state_dir: Path, task_id: str | None, plan_run_id: str) -> Path:
    return state_dir / "plan_runs" / (task_id or "_none") / f"{plan_run_id}.json"


def handoff_path(state_dir: Path, task_id: str, plan_id: str) -> Path:
    return state_dir / "handoffs" / task_id / f"{plan_id}.json"
