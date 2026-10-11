"""Synthetic research repositories with deliberately different layouts for Phase 7A tests."""

import json
from pathlib import Path

from research_autopilot.catalog import adapters
from research_autopilot.catalog.models import AdapterKind, AdapterOrigin
from research_autopilot.catalog.service import CatalogContext
from research_autopilot.implementation.models import RepoConfig
from research_autopilot.storage.db import Store

HUMAN = "rikushi"


def write_json(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


# ---- Repository A: "condition result + per-subset cache" (shape of a CASCI study) ----

def casci_repo(root: Path, commits=("aaaaaaa1111",), conditions=(("H2", "h2-r0.7", 2), ("H2", "h2-r0.7", 3))) -> Path:
    for commit in commits:
        c7 = commit[:7]
        for mol, geo, k in conditions:
            # Distinct subsets of size k (a subset has one energy, as in a real per-subset cache).
            evals = [{"subset": [0, i + 1, *range(10, 8 + k)], "energy": -1.0 - 0.01 * i - 0.001 * k}
                     for i in range(3)]
            for e in evals:
                write_json(root / f"cache/{c7}/{mol}/{geo}/{'-'.join(map(str, e['subset']))}-{k}.json",
                           {"key": {"subset": e["subset"], "ns": f"cfg-{commit}"}, "energy": e["energy"], "k": k})
            write_json(root / f"results/run_{c7}/{geo}/cond_k{k}.json", {
                "molecule": mol, "geometry": geo, "k": k, "commit": commit, "config": f"cfg-{commit}",
                "evaluations": evals, "optimum": min(e["energy"] for e in evals)})
    return root


CASCI_ADAPTER = {
    "name": "toy-casci", "version": "1.0.0",
    "artifacts": [{
        "rule": "condition", "kind": "toy-condition",
        "pattern": "results/run_{run}/{geometry_id}/cond_k{file_k}.json",
        "captures": {"run": "[0-9a-f]{7}"},
        "fields": {"molecule": "/molecule", "doc_geometry": "/geometry", "k": "/k", "commit": "/commit",
                   "commit7": {"pointer": "/commit", "prefix": 7}, "config": "/config",
                   "evaluations": "/evaluations", "optimum": "/optimum"},
        "identity": ["molecule", "geometry_id", "k"],
        "provenance": ["commit", "config"],
        "metadata": ["optimum"],
        "checks": [
            {"type": "equals_field", "field": "doc_geometry", "other": "geometry_id"},
            {"type": "equals_field", "field": "file_k", "other": "k"},
            {"type": "equals_field", "field": "commit7", "other": "run"},
            {"type": "items_finite", "field": "evaluations", "item": "/energy"},
            {"type": "items_min_equals", "field": "evaluations", "item": "/energy", "other": "optimum",
             "tolerance": 1e-12},
            {"type": "cross_reference", "field": "evaluations", "item": "/subset", "item_value": "/energy",
             "target_pattern": "cache/{commit7}/{molecule}/{geometry_id}/*.json", "target_key": "/key/subset",
             "target_value": "/energy", "target_match": {"/key/ns": "config"}, "tolerance": 1e-12}],
    }],
}


# ---- Repository B: "dataset shards" (several files form one artifact; no relation to chemistry) ----

def shard_repo(root: Path, n_shards: int = 3) -> Path:
    for split in ("train", "test"):
        for i in range(n_shards):
            write_json(root / f"data/v2/{split}/part-{i:03d}.json",
                       {"split": split, "index": i, "rows": 10, "schema": "tabular-v2", "ids": [i, i + 100]})
    return root


SHARD_ADAPTER = {
    "name": "toy-shards", "version": "1.0.0",
    "artifacts": [{
        "rule": "split", "kind": "tabular-split",
        "pattern": "data/v{version}/{split}/*.json",
        "fields": {"doc_split": "/split", "index": "/index", "schema": "/schema", "ids": "/ids", "rows": "/rows"},
        "identity": ["version", "split"],
        "metadata": ["schema"],
        "derive": {"shards": {"op": "count"}, "max_rows": {"op": "max", "field": "rows"}},
        "checks": [{"type": "equals_field", "field": "doc_split", "other": "split"},
                   {"type": "equals", "field": "schema", "value": "tabular-v2"},
                   {"type": "distinct", "field": "index"},
                   {"type": "sorted_unique", "field": "ids"},
                   {"type": "count", "count": 3}],
    }],
}


def context(tmp_path: Path, repos: dict[str, Path], environment: str = "mac") -> CatalogContext:
    store = Store(f"sqlite:///{tmp_path / 'catalog.db'}")
    store.init_schema()
    registry = {rid: RepoConfig(id=rid, path=str(p)) for rid, p in repos.items()}
    return CatalogContext(store=store, registry=registry, environment=environment)


def install(ctx: CatalogContext, definition: dict, repo_ids: list[str], *, activate: bool = True):
    rec = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=HUMAN,
                            definition=definition)
    rec = adapters.validate(ctx.store, rec.adapter_id, repository_id=repo_ids[0], root=ctx.root(repo_ids[0]),
                            actor=HUMAN)
    if activate:
        rec = adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)
    for rid in repo_ids:
        adapters.bind(ctx.store, rid, [definition["name"]], actor=HUMAN, registry=ctx.registry)
    return rec
