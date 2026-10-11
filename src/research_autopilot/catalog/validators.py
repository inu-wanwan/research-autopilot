"""Named group validators that declarative adapters may reference.

A declarative adapter can only *name* a validator; it can never supply code.
Validators are trusted RA code, versioned by their id, and receive read-only
member data that the engine already extracted.  Domain-specific checks (e.g.
for quantum chemistry) are added here, not in the catalog core.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from research_autopilot.catalog.models import CheckResult

# A member is the dict of fields extracted from one file (plus path captures).
Members = list[dict[str, Any]]
Validator = Callable[[Members, dict[str, Any]], CheckResult]

_REGISTRY: dict[str, Validator] = {}


def register(name: str) -> Callable[[Validator], Validator]:
    def wrap(fn: Validator) -> Validator:
        if name in _REGISTRY:
            raise ValueError(f"duplicate validator {name}")
        _REGISTRY[name] = fn
        return fn
    return wrap


def get(name: str) -> Validator | None:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@register("minimum_tie_report@1")
def minimum_tie_report(members: Members, params: dict[str, Any]) -> CheckResult:
    """Report whether the minimum of ``field`` is unique within ``tolerance``.

    Ties are not an integrity failure (the research task decides how to treat
    them), so the default is non-mandatory and the detail names the tie size.
    """
    field = params["field"]
    tol = float(params.get("tolerance", 0.0))
    values = [_num(m.get(field)) for m in members]
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite or len(finite) != len(values):
        return CheckResult(name=f"minimum_tie:{field}", passed=False, detail="non-numeric or non-finite values",
                           validator="minimum_tie_report@1", mandatory=bool(params.get("mandatory", False)))
    best = min(finite)
    ties = sum(1 for v in finite if v - best <= tol)
    return CheckResult(name=f"minimum_tie:{field}", passed=ties == 1,
                       detail=f"minimum {best!r}; {ties} member(s) within {tol}",
                       validator="minimum_tie_report@1", mandatory=bool(params.get("mandatory", False)))


@register("monotone_by_field@1")
def monotone_by_field(members: Members, params: dict[str, Any]) -> CheckResult:
    """Values of ``field`` must be non-increasing (or non-decreasing) when sorted by ``order``."""
    field, order = params["field"], params["order"]
    direction = params.get("direction", "non_increasing")
    tol = float(params.get("tolerance", 0.0))
    pairs = sorted((m.get(order), _num(m.get(field))) for m in members)
    bad = []
    for (o1, v1), (o2, v2) in zip(pairs, pairs[1:]):
        if v1 is None or v2 is None:
            bad.append(f"{o1}->{o2}: non-numeric")
        elif direction == "non_increasing" and v2 > v1 + tol:
            bad.append(f"{o1}->{o2}: {v1} -> {v2}")
        elif direction == "non_decreasing" and v2 < v1 - tol:
            bad.append(f"{o1}->{o2}: {v1} -> {v2}")
    return CheckResult(name=f"monotone:{field}", passed=not bad, detail="; ".join(bad[:5]),
                       validator="monotone_by_field@1", mandatory=bool(params.get("mandatory", True)))
