import threading

import pytest

from research_autopilot.idempotency import IdempotencyConflict, apply_once


def test_first_apply_then_already_applied(tmp_path):
    assert apply_once(tmp_path, "op-1", {"x": 1}) == "applied"
    assert apply_once(tmp_path, "op-1", {"x": 1}) == "already_applied"
    assert [p.name for p in tmp_path.iterdir()] == ["op-1.json"]


def test_conflicting_payload_is_detected(tmp_path):
    apply_once(tmp_path, "op-1", {"x": 1})
    with pytest.raises(IdempotencyConflict):
        apply_once(tmp_path, "op-1", {"x": 2})


def test_concurrent_duplicates_apply_exactly_once(tmp_path):
    n = 16
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = apply_once(tmp_path, "op-concurrent", {"x": 1})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("applied") == 1
    assert results.count("already_applied") == n - 1
    # no temp files left behind
    assert [p.name for p in tmp_path.iterdir()] == ["op-concurrent.json"]


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", ".hidden", "x" * 200])
def test_rejects_unsafe_operation_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        apply_once(tmp_path, bad, {})
