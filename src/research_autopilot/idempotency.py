"""Idempotent external side effects keyed by a stable operation id.

The side effect itself is the atomic creation of one file named after the
operation id, so there is no window between "effect done" and "effect
recorded". Concurrent duplicates race on ``os.link``, which fails for all but
one caller.

This does NOT give exactly-once semantics for arbitrary external services.
A real adapter must use the remote system's idempotency key or a unique
constraint; otherwise a crash between the remote call and its local record
can still duplicate the effect.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ApplyResult = Literal["applied", "already_applied"]


class IdempotencyConflict(Exception):
    """The operation id was already applied with a different payload."""


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def apply_once(state_dir: Path, operation_id: str, payload: dict[str, Any]) -> ApplyResult:
    if not _OPERATION_ID.match(operation_id):
        raise ValueError(f"invalid operation_id: {operation_id!r}")
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / f"{operation_id}.json"
    data = _encode(payload)

    fd, tmp_name = tempfile.mkstemp(dir=state_dir, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_name, target)
        except FileExistsError:
            if target.read_bytes() != data:
                raise IdempotencyConflict(operation_id) from None
            return "already_applied"
    finally:
        os.unlink(tmp_name)

    dir_fd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return "applied"


def state_dir_from_env() -> Path:
    return Path(os.environ.get("RA_STATE_DIR", ".prefect/research-autopilot-state"))
