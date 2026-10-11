"""Declarative adapter: a data-only description of how a repository stores artifacts.

The definition can name path patterns, JSON pointers, grouping, provenance
fields and a fixed vocabulary of checks.  It cannot run programs, import
modules, follow symlinks out of the repository, or address absolute paths, so
an AI-generated definition (Phase 7B) has the same capabilities as a human one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_autopilot.catalog import validators
from research_autopilot.catalog.models import CheckResult

MAX_MEMBER_BYTES = 16 * 1024 * 1024
_SEGMENT = re.compile(r"^(?:(?P<pre>[^{}*?\[\]/]*)\{(?P<name>[a-z_][a-z0-9_]*)\}(?P<post>[^{}*?\[\]/]*)"
                      r"|(?P<lit>[^{}*?\[\]]+)|\*|\*\.[A-Za-z0-9]+)$")


def _confine_pattern(v: str) -> str:
    if v.startswith(("/", "~")) or "\\" in v:
        raise ValueError("pattern must be repository-relative")
    for p in v.split("/"):
        if p in ("", ".", "..") or not _SEGMENT.match(p):
            raise ValueError(f"unsupported pattern segment {p!r}")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    pointer: str = Field(description='RFC 6901 JSON pointer into the member file, e.g. "/result/energy"; "" = whole doc')
    transform: Literal["none", "length", "sorted", "str", "int", "float"] = "none"
    prefix: int | None = None  # keep only the first N characters of a string (e.g. a short commit id)
    item: str | None = Field(None, description='for a list value: take this pointer from every element ("" = element)')
    required: bool = True


class CheckSpec(_Strict):
    """One deterministic check. ``type`` selects a fixed engine primitive."""

    type: Literal["equals", "one_of", "uniform", "equals_field", "finite", "distinct", "count", "validator",
                  "sorted_unique", "items_equal", "items_finite", "items_subset", "items_min_equals",
                  "cross_reference"]
    field: str | None = None
    other: str | None = None
    value: Any = None
    item: str | None = None        # items_*: JSON pointer inside each element of the list ``field``
    tolerance: float = 0.0
    # cross_reference: each list element of ``field`` must match a file found via ``target_pattern``
    # ({name} placeholders are member fields); ``target_key``/``target_value`` are JSON pointers in the
    # target files, ``item`` / ``item_value`` the matching pointers in the list element, ``target_match``
    # maps target pointers to member fields that must be equal (e.g. a config namespace).
    target_pattern: str | None = None
    target_key: str | None = None
    target_value: str | None = None
    item_value: str | None = None
    target_match: dict[str, str] = Field(default_factory=dict)
    target_equals: dict[str, Any] = Field(default_factory=dict)  # target pointer -> required constant
    count: int | None = None
    binomial: list[str] | None = None  # [n_field, k_field]: member count must equal C(n, k)
    validator: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    mandatory: bool = True

    @model_validator(mode="after")
    def _shape(self):
        need = {"equals": ("field",), "one_of": ("field", "value"), "uniform": ("field",),
                "equals_field": ("field", "other"), "finite": ("field",), "distinct": ("field",),
                "validator": ("validator",), "sorted_unique": ("field",), "count": (),
                "items_equal": ("field", "item"), "items_finite": ("field", "item"),
                "items_subset": ("field", "item", "other"), "items_min_equals": ("field", "item", "other"),
                "cross_reference": ("field", "item", "item_value", "target_pattern", "target_key",
                                    "target_value")}[self.type]
        pointers = {"item", "item_value", "target_key", "target_value"}  # "" is a valid pointer (whole element)
        missing = [n for n in need if getattr(self, n) is None or (getattr(self, n) == "" and n not in pointers)]
        if missing:
            raise ValueError(f"check {self.type} requires {missing}")
        if self.type == "count" and (self.count is None) == (self.binomial is None):
            raise ValueError("count check needs exactly one of count / binomial")
        if self.binomial is not None and len(self.binomial) != 2:
            raise ValueError("binomial must be [n_field, k_field]")
        if self.type == "one_of" and not isinstance(self.value, list):
            raise ValueError("one_of needs a list value")
        if self.target_pattern is not None:
            _confine_pattern(self.target_pattern)
        return self


class DeriveSpec(_Strict):
    op: Literal["min", "max", "argmin", "argmax", "count"]
    field: str | None = None
    of: str | None = None          # argmin/argmax: report this field of the extreme member(s)
    tolerance: float = 0.0         # argmin/argmax: members within tolerance are all reported


class ArtifactRule(_Strict):
    rule: str
    kind: str
    pattern: str = Field(description='repository-relative path pattern; each "/"-separated segment is a literal, '
                                     '"{name}" (captures the segment), "prefix{name}suffix", "*" or "*.ext"')
    format: Literal["json"] = "json"
    captures: dict[str, str] = Field(default_factory=dict, description=(
        "optional REGULAR EXPRESSIONS that a captured segment must fully match, keyed by capture name "
        '(e.g. {"run": "[0-9a-f]{7}"}); files whose capture does not match are skipped. Not a rename map.'))
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    identity: list[str] = Field(description=(
        "capture/field names that define the research condition; files with equal identity (and provenance) form "
        "ONE artifact (several member files when they share it)"))
    provenance: list[str] = Field(default_factory=list, description=(
        "names describing origin (run, commit, config hash); must be identical within an artifact; different "
        "values make separate artifacts"))
    metadata: list[str] = Field(default_factory=list, description=(
        "names copied into artifact metadata; must be IDENTICAL across all member files of an artifact (otherwise "
        "the artifact is inconsistent) - per-file values belong in derive or checks, not here"))
    derive: dict[str, DeriveSpec] = Field(default_factory=dict)
    checks: list[CheckSpec] = Field(default_factory=list)

    @field_validator("pattern")
    @classmethod
    def _confined(cls, v: str) -> str:
        return _confine_pattern(v)

    @field_validator("fields", mode="before")
    @classmethod
    def _short_fields(cls, v):
        return {k: ({"pointer": x} if isinstance(x, str) else x) for k, x in (v or {}).items()}

    @model_validator(mode="after")
    def _names(self):
        caps = set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", self.pattern))
        known = caps | set(self.fields)
        unknown = [n for n in (*self.identity, *self.provenance, *self.metadata) if n not in known]
        for c in self.checks:
            unknown += [n for n in (c.field, c.other, *(c.binomial or []), *c.target_match.values(),
                                    *re.findall(r"\{([a-z_][a-z0-9_]*)\}", c.target_pattern or ""))
                        if n and n not in known]
            if c.type == "validator" and validators.get(c.validator) is None:
                raise ValueError(f"unknown validator {c.validator}")
        for d in self.derive.values():
            unknown += [n for n in (d.field, d.of) if n and n not in known]
        if unknown:
            raise ValueError(f"unknown field/capture names: {sorted(set(unknown))}")
        if not self.identity:
            raise ValueError("identity must name at least one field")
        if set(self.captures) - caps:
            raise ValueError("captures constrain names that are not in the pattern")
        if clash := sorted(caps & set(self.fields)):
            raise ValueError(f"field names shadow path captures {clash}; rename the fields (e.g. doc_{clash[0]})")
        for name, rx in self.captures.items():
            try:
                re.compile(rx)
            except re.error as exc:
                raise ValueError(f"capture {name} is not a valid regular expression: {exc}") from exc
        return self


class DeclarativeDefinition(_Strict):
    name: str
    version: str
    description: str = ""
    artifacts: list[ArtifactRule]

    @model_validator(mode="after")
    def _unique(self):
        rules = [a.rule for a in self.artifacts]
        if len(rules) != len(set(rules)) or not rules:
            raise ValueError("artifact rules must be non-empty and uniquely named")
        return self


# ---------------------------------------------------------------- discovery


class DiscoveredGroup(BaseModel):
    """Repository-agnostic output of an adapter, consumed by the catalog."""

    rule: str
    kind: str
    identity: dict[str, Any]
    metadata: dict[str, Any]
    provenance: dict[str, Any]
    relative_dir: str
    members: dict[str, str]  # path relative to repo root -> sha256
    checks: list[CheckResult]


def iter_matches(root: Path, pattern: str) -> Iterator[tuple[Path, dict[str, str]]]:
    """Walk only the directories the pattern allows; never follow symlinks."""
    segs = pattern.split("/")

    def walk(base: Path, i: int, caps: dict[str, str]):
        last = i == len(segs) - 1
        seg = segs[i]
        m = _SEGMENT.match(seg)
        if m.group("lit") is not None:
            nxt = base / seg
            if nxt.is_symlink():
                return
            if last and nxt.is_file():
                yield nxt, caps
            elif not last and nxt.is_dir():
                yield from walk(nxt, i + 1, caps)
            return
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        for e in entries:
            if e.is_symlink() or e.name.startswith("."):
                continue
            if seg.startswith("*.") and not e.name.endswith(seg[1:]):
                continue
            new = dict(caps)
            if m.group("name"):
                pre, post = m.group("pre") or "", m.group("post") or ""
                if not e.name.startswith(pre) or not e.name.endswith(post) or len(e.name) <= len(pre) + len(post):
                    continue
                new[m.group("name")] = e.name[len(pre):len(e.name) - len(post)]
            if last and e.is_file(follow_symlinks=False):
                yield Path(e.path), new
            elif not last and e.is_dir(follow_symlinks=False):
                yield from walk(Path(e.path), i + 1, new)

    yield from walk(root, 0, {})


def _pointer(doc: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur = cur[int(tok)]
        else:
            raise KeyError(pointer)
    return cur


def _transform(value: Any, how: str) -> Any:
    if how == "none":
        return value
    if how == "length":
        return len(value)
    if how == "sorted":
        return sorted(value)
    return {"str": str, "int": int, "float": float}[how](value)


def _freeze(v: Any) -> Any:
    return json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v


def discover(definition: DeclarativeDefinition, root: Path) -> tuple[list[DiscoveredGroup], list[str]]:
    root = root.expanduser().resolve()
    groups: list[DiscoveredGroup] = []
    errors: list[str] = []
    for rule in definition.artifacts:
        regex = {k: re.compile(v) for k, v in rule.captures.items()}
        buckets: dict[tuple, list[tuple[str, str, dict[str, Any]]]] = {}
        for path, caps in iter_matches(root, rule.pattern):
            if any(not r.fullmatch(caps.get(k, "")) for k, r in regex.items()):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                if path.stat().st_size > MAX_MEMBER_BYTES:
                    errors.append(f"{rule.rule}: {rel} exceeds {MAX_MEMBER_BYTES} bytes")
                    continue
                data = path.read_bytes()
                doc = json.loads(data)
            except (OSError, ValueError) as exc:
                errors.append(f"{rule.rule}: {rel} unreadable ({type(exc).__name__})")
                continue
            values: dict[str, Any] = dict(caps)
            missing = []
            for name, spec in rule.fields.items():
                try:
                    value = _pointer(doc, spec.pointer)
                    if spec.item is not None:
                        value = [_pointer(el, spec.item) for el in value]
                    value = _transform(value, spec.transform)
                    values[name] = value[:spec.prefix] if spec.prefix and isinstance(value, str) else value
                except (KeyError, TypeError, ValueError):
                    if spec.required:
                        missing.append(name)
                    values[name] = None
            values["__missing__"] = missing
            # Same research condition from a different source (run, config) is a different artifact;
            # the catalog reports it as ambiguous at resolution instead of merging it here.
            key = tuple(_freeze(values.get(n)) for n in (*rule.identity, *rule.provenance))
            buckets.setdefault(key, []).append((rel, hashlib.sha256(data).hexdigest(), values))
        ctx = _Context(root)
        for key, items in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            groups.append(_group(rule, key, items, ctx))
    return groups, errors


class _Context:
    """Read-only, repository-confined lookups shared by the checks of one scan."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[tuple, dict[str, Any] | str] = {}

    def target_index(self, c: CheckSpec, member: dict[str, Any]) -> dict[str, Any] | str:
        """Map target_key -> (target_value, target doc matches) for one resolved target pattern."""
        try:
            pattern = re.sub(r"\{([a-z_][a-z0-9_]*)\}", lambda m: _segment_value(member[m.group(1)]), c.target_pattern)
        except (KeyError, ValueError) as exc:
            return f"cannot resolve target pattern ({exc})"
        match = tuple(sorted([(ptr, _freeze(member.get(f))) for ptr, f in c.target_match.items()]
                             + [(ptr, _freeze(v)) for ptr, v in c.target_equals.items()]))
        key = (pattern, c.target_key, c.target_value, match)
        if key not in self._index:
            index: dict[str, Any] = {}
            for path, _ in iter_matches(self.root, pattern):
                try:
                    if path.stat().st_size > MAX_MEMBER_BYTES:
                        continue
                    doc = json.loads(path.read_bytes())
                    if any(_freeze(_pointer(doc, ptr)) != want for ptr, want in match):
                        continue
                    k = _freeze(_pointer(doc, c.target_key))
                    v = _pointer(doc, c.target_value)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                index[k] = "__duplicate__" if k in index else v
            self._index[key] = index or f"no matching target files for {pattern}"
        return self._index[key]


def _segment_value(value: Any) -> str:
    text = str(value)
    if not text or "/" in text or text in (".", "..") or "\\" in text:
        raise ValueError(f"unsafe path segment {text!r}")
    return text


def _uniform(values: list[Any]) -> tuple[bool, Any]:
    frozen = {_freeze(v) for v in values}
    return len(frozen) == 1, values[0] if values else None


def _group(rule: ArtifactRule, key: tuple, items: list[tuple[str, str, dict[str, Any]]],
           ctx: _Context) -> DiscoveredGroup:
    members = [v for _, _, v in items]
    identity = {n: members[0].get(n) for n in rule.identity}
    checks: list[CheckResult] = []
    missing: dict[str, int] = {}
    for _, _, v in items:
        for n in v["__missing__"]:
            missing[n] = missing.get(n, 0) + 1
    checks.append(CheckResult(name="required_fields", passed=not missing, validator="engine@1",
                              detail=", ".join(f"{n} missing in {c} member(s)" for n, c in sorted(missing.items()))))
    out: dict[str, dict[str, Any]] = {"provenance": {}, "metadata": {}}
    for section, names_ in (("provenance", rule.provenance), ("metadata", rule.metadata)):
        for n in names_:
            same, value = _uniform([m.get(n) for m in members])
            out[section][n] = value if same else None
            if not same:
                checks.append(CheckResult(name=f"uniform:{n}", passed=False, detail=f"{section} field differs",
                                          validator="engine@1"))
    for c in rule.checks:
        checks.append(_check(c, members, ctx))
    for name, d in rule.derive.items():
        out["metadata"][name] = _derive(d, members)
    dirs = {rel.rsplit("/", 1)[0] if "/" in rel else "" for rel, _, _ in items}
    common = os.path.commonpath(list(dirs)) if dirs and all(dirs) else ""
    return DiscoveredGroup(rule=rule.rule, kind=rule.kind, identity=identity, metadata=out["metadata"],
                           provenance=out["provenance"], relative_dir=common,
                           members={rel: digest for rel, digest, _ in items}, checks=checks)


def _same(a: Any, b: Any) -> bool:
    """Equality across JSON values and path captures: "000" (a captured segment) equals 0, "2" equals 2."""
    if a == b or (isinstance(a, str) and isinstance(b, str) and a == b):
        return True
    if str(a) == str(b):
        return True
    try:
        return not isinstance(a, bool) and not isinstance(b, bool) and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _items(member: dict[str, Any], c: CheckSpec, pointer: str) -> list[Any] | None:
    seq = member.get(c.field)
    if not isinstance(seq, list):
        return None
    out = []
    for el in seq:
        try:
            out.append(_pointer(el, pointer))
        except (KeyError, TypeError):
            out.append(_MISSING)
    return out


_MISSING = object()


def _finite(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _check(c: CheckSpec, members: list[dict[str, Any]], ctx: _Context) -> CheckResult:
    def res(name: str, ok: bool, detail: str = "") -> CheckResult:
        return CheckResult(name=name, passed=ok, detail=detail, mandatory=c.mandatory, validator="engine@1")

    vals = [m.get(c.field) for m in members] if c.field else []
    if c.type == "equals":
        bad = [v for v in vals if v != c.value]
        return res(f"equals:{c.field}", not bad, f"{len(bad)} member(s) differ from {c.value!r}" if bad else "")
    if c.type == "one_of":
        bad = [v for v in vals if v not in c.value]
        return res(f"one_of:{c.field}", not bad, f"unexpected value(s) {sorted(map(str, bad))[:3]}" if bad else "")
    if c.type.startswith("items_") or c.type == "cross_reference":
        return res(f"{c.type}:{c.field}{c.item}", *_list_check(c, members, ctx))
    if c.type == "uniform":
        same, _ = _uniform(vals)
        return res(f"uniform:{c.field}", same)
    if c.type == "equals_field":
        bad = sum(1 for m in members if not _same(m.get(c.field), m.get(c.other)))
        return res(f"equals_field:{c.field}={c.other}", not bad, f"{bad} member(s) differ" if bad else "")
    if c.type == "finite":
        bad = sum(1 for v in vals if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v))
        return res(f"finite:{c.field}", not bad, f"{bad} non-finite value(s)" if bad else "")
    if c.type in ("distinct", "sorted_unique"):
        frozen = [_freeze(v) for v in vals]
        dup = len(frozen) - len(set(frozen))
        ok = not dup
        if c.type == "sorted_unique":
            unsorted = sum(1 for v in vals if not isinstance(v, list) or v != sorted(v) or len(v) != len(set(v)))
            ok = ok and not unsorted
            return res(f"sorted_unique:{c.field}", ok, f"{dup} duplicate(s), {unsorted} unsorted/repeated")
        return res(f"distinct:{c.field}", ok, f"{dup} duplicate value(s)" if dup else "")
    if c.type == "count":
        if c.count is not None:
            return res("count", len(members) == c.count, f"{len(members)} member(s), expected {c.count}")
        n_ok, n = _uniform([m.get(c.binomial[0]) for m in members])
        k_ok, k = _uniform([m.get(c.binomial[1]) for m in members])
        if not (n_ok and k_ok) or not isinstance(n, int) or not isinstance(k, int):
            return res("count:binomial", False, "n/k are not uniform integers")
        expected = math.comb(n, k)
        return res("count:binomial", len(members) == expected, f"{len(members)} member(s), expected C({n},{k})={expected}")
    fn = validators.get(c.validator)
    r = fn(members, {**c.params, "mandatory": c.mandatory})
    r.mandatory = c.mandatory
    return r


def _list_check(c: CheckSpec, members: list[dict[str, Any]], ctx: _Context) -> tuple[bool, str]:
    problems: list[str] = []
    checked = 0
    for m in members:
        items = _items(m, c, c.item)
        if items is None:
            problems.append(f"{c.field} is not a list")
            continue
        if any(x is _MISSING for x in items):
            problems.append(f"{sum(x is _MISSING for x in items)} element(s) lack {c.item}")
            continue
        if c.type == "items_equal":
            bad = sum(1 for x in items if x != c.value)
            if bad:
                problems.append(f"{bad} element(s) differ from {c.value!r}")
        elif c.type == "items_finite":
            bad = sum(1 for x in items if not _finite(x))
            if bad:
                problems.append(f"{bad} non-finite element(s)")
        elif c.type == "items_subset":
            allowed = m.get(c.other)
            allowed = {_freeze(a) for a in allowed} if isinstance(allowed, list) else set()
            bad = sum(1 for x in items for y in (x if isinstance(x, list) else [x]) if _freeze(y) not in allowed)
            if bad:
                problems.append(f"{bad} value(s) outside {c.other}")
        elif c.type == "items_min_equals":
            target = m.get(c.other)
            finite = [x for x in items if _finite(x)]
            if not finite or not _finite(target) or abs(min(finite) - target) > c.tolerance:
                problems.append(f"min of {c.item} {min(finite) if finite else None!r} != {c.other} {target!r}")
        else:  # cross_reference
            index = ctx.target_index(c, m)
            if isinstance(index, str):
                problems.append(index)
                continue
            values = _items(m, c, c.item_value) or []
            missing = mismatch = dup = 0
            for k, v in zip(items, values):
                hit = index.get(_freeze(k))
                if hit is None:
                    missing += 1
                elif hit == "__duplicate__":
                    dup += 1
                elif not (_finite(v) and _finite(hit) and abs(v - hit) <= c.tolerance) and v != hit:
                    mismatch += 1
            checked += len(items)
            if missing or mismatch or dup:
                problems.append(f"{missing} missing, {mismatch} mismatched, {dup} duplicated in target")
    if c.type == "cross_reference" and not problems:
        return True, f"{checked} element(s) matched in target"
    return not problems, "; ".join(sorted(set(problems))[:3])


def _derive(d: DeriveSpec, members: list[dict[str, Any]]) -> Any:
    if d.op == "count":
        return len(members)
    pairs = [(m.get(d.field), m) for m in members
             if isinstance(m.get(d.field), (int, float)) and math.isfinite(m.get(d.field))]
    if not pairs:
        return None
    if d.op in ("min", "max"):
        return (min if d.op == "min" else max)(v for v, _ in pairs)
    best = (min if d.op == "argmin" else max)(v for v, _ in pairs)
    near = [m for v, m in pairs if abs(v - best) <= d.tolerance]
    return sorted((m.get(d.of) for m in near), key=lambda x: json.dumps(x, sort_keys=True))


def parse(definition: dict[str, Any]) -> DeclarativeDefinition:
    return DeclarativeDefinition.model_validate(definition)


CHECK_VOCABULARY: dict[str, dict[str, str]] = {
    "equals": {"needs": "field, value", "means": "every member's field == value"},
    "one_of": {"needs": "field, value (list)", "means": "every member's field is one of value"},
    "uniform": {"needs": "field", "means": "field has the same value in all members of the artifact"},
    "equals_field": {"needs": "field, other", "means": "field == other per member (other may be a path capture)"},
    "finite": {"needs": "field", "means": "field is a finite number"},
    "distinct": {"needs": "field", "means": "no two members share the value"},
    "sorted_unique": {"needs": "field", "means": "field is a sorted list without repeats, distinct across members"},
    "count": {"needs": "count OR binomial [n_field, k_field]", "means": "number of member files"},
    "items_equal": {"needs": "field (a list), item (pointer in each element, \"\" = element), value",
                    "means": "every element's item == value"},
    "items_finite": {"needs": "field (a list), item", "means": "every element's item is a finite number"},
    "items_subset": {"needs": "field (a list), item, other (a list field)", "means": "element values are in other"},
    "items_min_equals": {"needs": "field (a list), item, other, tolerance",
                         "means": "min over elements' item equals other within tolerance"},
    "cross_reference": {"needs": "field (a list), item, item_value, target_pattern, target_key, target_value; "
                                 "optional target_match {pointer: member_field}, target_equals {pointer: constant}, "
                                 "tolerance",
                        "means": "each element's item is found as target_key in files matched by target_pattern "
                                 "({name} placeholders are member fields) and its item_value equals target_value"},
    "validator": {"needs": "validator (a NAMED_VALIDATORS entry), params", "means": "named trusted check"},
}


def diagnose(definition: DeclarativeDefinition, root: Path) -> list[str]:
    """Explain, per rule, where discovery loses files (for generator feedback and humans)."""
    root = root.expanduser().resolve()
    out = []
    for rule in definition.artifacts:
        regex = {k: re.compile(v) for k, v in rule.captures.items()}
        matched = kept = 0
        missing: dict[str, int] = {}
        hints: dict[str, str] = {}
        for path, caps in iter_matches(root, rule.pattern):
            matched += 1
            if any(not r.fullmatch(caps.get(k, "")) for k, r in regex.items()):
                continue
            kept += 1
            if kept > 200:
                continue
            try:
                doc = json.loads(path.read_bytes())
            except (OSError, ValueError):
                missing["<unreadable json>"] = missing.get("<unreadable json>", 0) + 1
                continue
            for name, spec in rule.fields.items():
                try:
                    _pointer(doc, spec.pointer)
                except (KeyError, TypeError):
                    if spec.required:
                        missing[name] = missing.get(name, 0) + 1
                        hints.setdefault(name, _pointer_hint(doc, spec.pointer))
        line = f"rule {rule.rule}: pattern matched {matched} file(s); {kept} kept after captures regexes"
        if matched == 0:
            line += f"; {_deepest_miss(root, rule.pattern)}"
        if missing:
            line += f"; required fields missing (in first 200): {missing}; pointer hints: {hints}"
        out.append(line)
    return out


def _pointer_hint(doc: Any, pointer: str) -> str:
    """Where a JSON pointer stops resolving, and which keys exist there."""
    cur, walked = doc, ""
    for raw in pointer.lstrip("/").split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and tok in cur:
            cur, walked = cur[tok], f"{walked}/{raw}"
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur, walked = cur[int(tok)], f"{walked}/{raw}"
        else:
            keys = sorted(cur)[:30] if isinstance(cur, dict) else f"<{type(cur).__name__}>"
            return f"{pointer!r} stops at {walked or '/'!r}, which has {keys}"
    return f"{pointer!r} resolves"


def _deepest_miss(root: Path, pattern: str) -> str:
    segs = pattern.split("/")
    for depth in range(len(segs), 0, -1):
        prefix = "/".join(segs[:depth])
        if next(iter_matches(root, prefix), None) is not None or any(
                True for _ in _dirs(root, segs[:depth])):
            nxt = segs[depth] if depth < len(segs) else ""
            return f"nothing matches segment {nxt!r} after {prefix!r}"
    return f"nothing matches the first segment {segs[0]!r}"


def _dirs(root: Path, segs: list[str]):
    """Directories matching a pattern prefix (iter_matches only yields files at the last segment)."""
    frontier = [root]
    for seg in segs:
        m = _SEGMENT.match(seg)
        nxt = []
        for base in frontier:
            if m and m.group("lit") is not None:
                cand = base / seg
                if cand.is_dir() and not cand.is_symlink():
                    nxt.append(cand)
                continue
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for e in entries:
                if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                    continue
                if m and m.group("name"):
                    pre, post = m.group("pre") or "", m.group("post") or ""
                    if not (e.name.startswith(pre) and e.name.endswith(post)):
                        continue
                nxt.append(Path(e.path))
        frontier = nxt[:500]
        if not frontier:
            return
    yield from frontier
