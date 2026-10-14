"""Deterministic validation of collected artifacts. exit code 0 alone never passes.

Status is ``pass`` only if every machine-checkable check passes. Acceptance
criteria and scientific invariants written in prose cannot be proven here; they are
reported as ``not_verified`` (``fully_verified=False``) unless a numeric check
explicitly names them -- they are never silently counted as passed.
"""

import csv
import hashlib
import io
import json
import math
from pathlib import Path

from research_autopilot.experiments.models import CollectResult, JobStatus, ValidationCheck, ValidationResult
from research_autopilot.implementation.experiment import ExperimentSpec


def _pointer(doc, pointer: str):
    if pointer in ("", "/"):
        return doc
    cur = doc
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _expand(doc, pointer: str) -> list[tuple[str, object]]:
    """JSON pointer with ``*`` segments expanded over list items / dict values: [(concrete pointer, value)]."""
    parts = pointer.lstrip("/").split("/") if pointer not in ("", "/") else []
    out = [("", doc)]
    for part in parts:
        nxt = []
        for path, cur in out:
            if part == "*":
                items = enumerate(cur) if isinstance(cur, list) else (cur.items() if isinstance(cur, dict) else [])
                nxt += [(f"{path}/{k}", v) for k, v in items]
            else:
                key = part.replace("~1", "/").replace("~0", "~")
                nxt.append((f"{path}/{part}", cur[int(key)] if isinstance(cur, list) else cur[key]))
        out = nxt
    return out


_UNIT_MAPS = {"units", "unit"}


def _find_key(obj, key: str, path: str = "") -> list[tuple[str, object]]:
    """Every occurrence of a field name in nested records (e.g. units declared per record of a results list)."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and (k in _UNIT_MAPS or (v and all(isinstance(x, str) for x in v.values()))):
                # {"units": {"e_hartree": "Hartree"}} / {"definitions": {"f": "1 - |A|/|Q|"}} document the fields;
                # they are not values of them (a record holding numbers is still checked)
                continue
            if k == key:
                hits.append((f"{path}/{k}", v))
            hits += _find_key(v, key, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _find_key(v, key, f"{path}/{i}")
    return hits


def _finite_number(raw) -> bool:
    return not isinstance(raw, bool) and isinstance(raw, (int, float)) and math.isfinite(float(raw))


def _quantity(raw) -> bool:
    """A value a unit can be declared for: a finite number, a non-empty list of such values (e.g. the scanned
    distances), an object or list whose non-label leaves are all finite (e.g. coordinates with an atom label:
    {"atom": "H", "z": 0.7} or PySCF's ["H", [0.0, 0.0, 0.7]]), or a boolean (a flag: the unit is meaningless
    and ignored, it is not a reason to reject the result).  String labels never count as the quantity: at least
    one numeric leaf is required."""
    if isinstance(raw, bool):
        return True
    if isinstance(raw, (int, float)):
        return math.isfinite(float(raw))
    if isinstance(raw, (list, dict)):
        leaves = [v for v in (raw.values() if isinstance(raw, dict) else raw) if not isinstance(v, str)]
        return bool(leaves) and all(_quantity(v) for v in leaves)
    return False


def _nonfinite(obj, path="") -> list[str]:
    bad = []
    if isinstance(obj, float) and not math.isfinite(obj):
        bad.append(path or "/")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            bad += _nonfinite(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += _nonfinite(v, f"{path}/{i}")
    return bad


def _load(path: Path, fmt: str):
    data = path.read_bytes()
    if fmt == "json":
        return json.loads(data, parse_constant=lambda c: float(c))  # NaN/Infinity -> float for detection
    if fmt == "csv":
        rows = list(csv.reader(io.StringIO(data.decode())))
        if not rows:
            raise ValueError("empty CSV")
        return rows
    if fmt in ("npy", "npz"):
        import numpy as np  # optional dependency of research repos; available with pyscf/numpy

        return np.load(io.BytesIO(data), allow_pickle=False)
    if fmt == "txt":
        return data.decode()
    return data


def _cmp(cond: str, x: float, y: float | None, tol: float | None) -> bool:
    if cond == "finite":
        return math.isfinite(x)
    t = tol or 0.0
    return {"lt": x < y + t, "le": x <= y + t, "gt": x > y - t, "ge": x >= y - t,
            "eq": abs(x - y) <= t, "abs_le": abs(x) <= y + t}[cond]


def validate(spec: ExperimentSpec, status: JobStatus, collected: CollectResult, workdir: Path,
             input_hashes: dict[str, str]) -> ValidationResult:
    checks: list[ValidationCheck] = []
    metrics: dict[str, float] = {}
    loaded: dict[str, object] = {}
    by_path = {a.path: a for a in collected.artifacts}

    checks.append(ValidationCheck(name="exit_status", kind="exit_status",
                                  status="pass" if status.state.value == "succeeded" and status.exit_code == 0 else "fail",
                                  detail=f"state={status.state.value} exit={status.exit_code} signal={status.signal}"))
    for out in spec.outputs:
        art = by_path.get(out.path)
        if art is None:
            checks.append(ValidationCheck(name=f"exists:{out.path}", kind="output_exists",
                                          status="fail" if out.required else "not_verified",
                                          detail="missing" + ("" if out.required else " (optional)")))
            continue
        checks.append(ValidationCheck(name=f"exists:{out.path}", kind="output_exists", status="pass",
                                      detail=f"{art.size_bytes} bytes sha256={art.sha256[:12]}"))
        try:
            doc = _load(Path(art.stored_path), out.format)
            loaded[out.path] = doc
            checks.append(ValidationCheck(name=f"format:{out.path}", kind="format", status="pass", detail=out.format))
        except Exception as exc:  # noqa: BLE001 - any parse error = corrupt artifact
            checks.append(ValidationCheck(name=f"format:{out.path}", kind="format", status="fail",
                                          detail=f"cannot parse as {out.format}: {type(exc).__name__}: {str(exc)[:150]}"))
            continue
        if out.format == "json":
            bad = _nonfinite(doc)
            checks.append(ValidationCheck(name=f"finite:{out.path}", kind="finite", status="fail" if bad else "pass",
                                          detail=f"non-finite values at {bad[:5]}" if bad else "no NaN/Inf"))
            for field, unit in out.units.items():
                # OutputFile.units permits top-level field names ("e_fci") as well as JSON pointers ("/e_fci").
                ptr = field if field.startswith("/") else "/" + field.replace("~", "~0").replace("/", "~1")
                try:
                    raw = _pointer(doc, ptr)
                    numeric = _finite_number(raw)
                    ok = _quantity(raw)
                    checks.append(ValidationCheck(
                        name=f"units:{out.path}:{field}", kind="units", status="pass" if ok else "fail",
                        detail=(f"numeric finite field; declared unit {unit}" if numeric else
                                f"finite numeric values ({type(raw).__name__}); declared unit {unit}" if ok else
                                f"field is not a finite number; declared unit {unit}"),
                        value=float(raw) if numeric else None,
                    ))
                except (KeyError, IndexError, ValueError, TypeError):
                    # not at the top level: the field may be declared per record (e.g. results[*].delta_e_hartree)
                    hits = _find_key(doc, field) if not field.startswith("/") else []
                    ok = bool(hits) and all(_quantity(v) for _, v in hits)
                    checks.append(ValidationCheck(
                        name=f"units:{out.path}:{field}", kind="units", status="pass" if ok else "fail",
                        detail=(f"{len(hits)} nested numeric finite occurrence(s); declared unit {unit}" if ok else
                                f"field with declared unit {unit} missing" if not hits else
                                f"nested field is not always a finite number; declared unit {unit}")))
        elif out.format in ("npy",):
            import numpy as np

            ok = bool(np.isfinite(doc).all()) if np.issubdtype(doc.dtype, np.number) else True
            checks.append(ValidationCheck(name=f"finite:{out.path}", kind="finite", status="pass" if ok else "fail",
                                          detail="all finite" if ok else "non-finite values"))

    for m in spec.validation.metrics:
        doc = loaded.get(m.output)
        if doc is not None and m.json_pointer and "*" in m.json_pointer.split("/"):
            try:  # one metric value per record: name[<concrete pointer>]
                vals = [(p, float(v)) for p, v in _expand(doc, m.json_pointer)]
            except (KeyError, IndexError, ValueError, TypeError):
                vals = []
            ok = bool(vals) and all(math.isfinite(v) for _, v in vals)
            for p, v in vals:
                metrics[f"{m.name}[{p}]"] = v
            checks.append(ValidationCheck(name=f"metric:{m.name}", kind="metric", status="pass" if ok else "fail",
                                          detail=(f"{len(vals)} value(s) from {m.json_pointer}: {m.definition}" if vals
                                                  else f"cannot extract {m.json_pointer} from {m.output}")))
            if m.units and vals:
                checks.append(ValidationCheck(name=f"metric_units:{m.name}", kind="units",
                                              status="pass" if ok else "fail",
                                              detail=f"metric extracted; declared unit {m.units}"))
            continue
        try:
            val = float(_pointer(doc, m.json_pointer)) if doc is not None and m.json_pointer else None
        except (KeyError, IndexError, ValueError, TypeError):
            val = None
        if val is None:
            checks.append(ValidationCheck(name=f"metric:{m.name}", kind="metric", status="fail",
                                          detail=f"cannot extract {m.json_pointer} from {m.output}"))
        else:
            metrics[m.name] = val
            checks.append(ValidationCheck(name=f"metric:{m.name}", kind="metric",
                                          status="pass" if math.isfinite(val) else "fail", detail=m.definition, value=val))
            if m.units:
                checks.append(ValidationCheck(name=f"metric_units:{m.name}", kind="units",
                                              status="pass" if math.isfinite(val) else "fail",
                                              detail=f"metric extracted; declared unit {m.units}", value=val))

    for c in spec.validation.numeric_checks:
        doc = loaded.get(c.output)
        if doc is not None and "*" in c.json_pointer.split("/"):
            try:  # the condition must hold for every record
                pairs = _expand(doc, c.json_pointer)
                refs = _expand(doc, c.reference_pointer) if c.reference_pointer else None
                if refs is not None and len(refs) != len(pairs):
                    raise ValueError("reference pointer does not expand to the same records")
                results = [(_cmp(c.condition, float(v), float(refs[i][1]) if refs else c.value, c.tolerance), p, v)
                           for i, (p, v) in enumerate(pairs)]
                ok = bool(results) and all(r[0] for r in results)
                bad = [p for r, p, _ in results if not r]
                checks.append(ValidationCheck(name=f"check:{c.name}", kind="numeric_check", status="pass" if ok else "fail",
                                              detail=f"{len(results)} record(s) {c.condition} "
                                                     f"{c.reference_pointer or c.value}" + (f"; failing {bad[:5]}" if bad else "")))
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                checks.append(ValidationCheck(name=f"check:{c.name}", kind="numeric_check", status="fail",
                                              detail=f"cannot evaluate: {type(exc).__name__}"))
            continue
        try:
            x = float(_pointer(doc, c.json_pointer))
            y = float(_pointer(doc, c.reference_pointer)) if c.reference_pointer else c.value
            ok = _cmp(c.condition, x, y, c.tolerance)
            checks.append(ValidationCheck(name=f"check:{c.name}", kind="numeric_check", status="pass" if ok else "fail",
                                          detail=f"{x} {c.condition} {y} (tol {c.tolerance})", value=x))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            checks.append(ValidationCheck(name=f"check:{c.name}", kind="numeric_check", status="fail",
                                          detail=f"cannot evaluate: {type(exc).__name__}"))

    for inp in spec.inputs:  # inputs must be byte-identical to the fixed commit
        p = workdir / inp.path
        actual = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        expected = input_hashes.get(inp.path)
        checks.append(ValidationCheck(name=f"input:{inp.path}", kind="input_integrity",
                                      status="pass" if actual and actual == expected else "fail",
                                      detail="unchanged from commit" if actual == expected else "modified or missing"))

    check_names = {c.name.lower() for c in spec.validation.numeric_checks}
    for crit in spec.validation.acceptance_criteria:
        checks.append(ValidationCheck(name=f"acceptance:{crit[:60]}", kind="acceptance_criterion", status="not_verified",
                                      detail="prose criterion; covered only partially by numeric checks "
                                             f"({sorted(check_names)}) -- requires analyst / human judgement"))
    for inv in spec.validation.scientific_invariants:
        checks.append(ValidationCheck(name=f"invariant:{inv[:60]}", kind="scientific_invariant", status="not_verified",
                                      detail="prose invariant; scientific inputs verified unchanged via input_integrity"))

    machine = [c for c in checks if c.kind not in ("acceptance_criterion", "scientific_invariant")]
    failed = [c for c in machine if c.status == "fail"]
    return ValidationResult(
        experiment_id=spec.experiment_id, job_key=collected.job_key, status="fail" if failed else "pass",
        fully_verified=not failed and all(c.status == "pass" for c in checks), checks=checks, metrics=metrics,
        failure_reason="; ".join(f"{c.name}: {c.detail}" for c in failed[:5]) or None,
    )
