import copy
import json
from pathlib import Path

import pytest
from catalog_fakes import CASCI_ADAPTER, HUMAN, SHARD_ADAPTER, casci_repo, context, install, shard_repo
from pydantic import ValidationError

from research_autopilot.catalog import adapters, declarative, service
from research_autopilot.catalog.adapters import AdapterError
from research_autopilot.catalog.models import (AdapterKind, AdapterOrigin, AdapterState, ArtifactQuery,
                                               ValidationStatus)
from research_autopilot.storage import db


def _q(text):
    return service.parse_requirements(text)


# ---------------------------------------------------------------- adapter lifecycle


def test_adapter_lifecycle_separates_registration_validation_and_activation(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    rec = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=HUMAN,
                            definition=CASCI_ADAPTER)
    adapters.bind(ctx.store, "casci", ["toy-casci"], actor=HUMAN, registry=ctx.registry)
    assert rec.state is AdapterState.REGISTERED
    assert service.scan(ctx, "casci").errors == ["no active adapter is bound to this repository"]
    with pytest.raises(AdapterError, match="validation"):
        adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)
    rec = adapters.validate(ctx.store, rec.adapter_id, repository_id="casci", root=ctx.root("casci"), actor=HUMAN)
    assert rec.state is AdapterState.VALIDATED and rec.validation.discovered == 2
    assert service.scan(ctx, "casci").discovered == 0  # validated is still not usable
    with pytest.raises(AdapterError, match="human"):
        adapters.activate(ctx.store, rec.adapter_id, actor=adapters.AUTOPILOT_ACTOR)
    rec = adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)
    assert rec.state is AdapterState.ACTIVE and [e.kind for e in rec.history] == [
        "registered", "validated", "activated"]
    assert service.scan(ctx, "casci").discovered == 2


def test_adapter_versions_are_immutable_and_activation_retires_the_previous_version(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    v1 = install(ctx, CASCI_ADAPTER, ["casci"])
    changed = copy.deepcopy(CASCI_ADAPTER)
    changed["artifacts"][0]["metadata"] = []
    with pytest.raises(AdapterError, match="bump the version"):
        adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=HUMAN,
                          definition=changed)
    changed["version"] = "1.1.0"
    v2 = install(ctx, changed, ["casci"])
    assert adapters.get(ctx.store, v1.adapter_id).state is AdapterState.RETIRED
    assert [a.adapter_id for a in adapters.active_for(ctx.store, "casci")] == [v2.adapter_id]


def test_failed_validation_rejects_and_never_activates(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    broken = copy.deepcopy(CASCI_ADAPTER)
    broken["artifacts"][0]["pattern"] = "nothing/{x}/*.json"
    broken["artifacts"][0]["identity"] = ["x"]
    broken["artifacts"][0]["provenance"] = broken["artifacts"][0]["metadata"] = []
    broken["artifacts"][0]["checks"] = []
    broken["artifacts"][0]["captures"] = {}
    rec = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL, actor=HUMAN,
                            definition=broken)
    rec = adapters.validate(ctx.store, rec.adapter_id, repository_id="casci", root=ctx.root("casci"), actor=HUMAN)
    assert rec.state is AdapterState.REJECTED
    with pytest.raises(AdapterError):
        adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)


def test_python_adapters_are_confined_to_the_trusted_package_and_generated_ones_never_run(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    outside = adapters.register(ctx.store, kind=AdapterKind.PYTHON, origin=AdapterOrigin.MANUAL, actor=HUMAN,
                                name="evil", version="1", entrypoint="os:system")
    rec = adapters.validate(ctx.store, outside.adapter_id, repository_id="casci", root=ctx.root("casci"), actor=HUMAN)
    assert rec.state is AdapterState.REJECTED and "trusted" in rec.validation.checks[0].detail
    generated = adapters.register(ctx.store, kind=AdapterKind.PYTHON, origin=AdapterOrigin.GENERATED, actor="ai",
                                  name="gen", version="1",
                                  entrypoint="research_autopilot.catalog.builtin.x:make",
                                  generator={"model": "fake"})
    with pytest.raises(AdapterError, match="never executed"):
        adapters.runtime(generated)
    # Phase 7B policy hook: only generated declarative adapters may skip the human.
    gen_decl = adapters.register(ctx.store, kind=AdapterKind.DECLARATIVE, origin=AdapterOrigin.GENERATED, actor="ai",
                                 definition={**SHARD_ADAPTER, "name": "gen-shards"})
    assert adapters.requires_human(generated) and not adapters.requires_human(gen_decl)


def test_declarative_definitions_cannot_escape_the_repository(tmp_path):
    base = copy.deepcopy(SHARD_ADAPTER)
    for bad in ("../outside/{split}/*.json", "/etc/{split}/*.json", "~/{split}/*.json", "data/**/{split}"):
        base["artifacts"][0]["pattern"] = bad
        with pytest.raises(ValidationError):
            declarative.parse(base)
    unknown = copy.deepcopy(SHARD_ADAPTER)
    unknown["artifacts"][0]["checks"] = [{"type": "validator", "validator": "os.system"}]
    with pytest.raises(ValidationError, match="unknown validator"):
        declarative.parse(unknown)
    extra = copy.deepcopy(SHARD_ADAPTER)
    extra["artifacts"][0]["exec"] = "rm -rf /"
    with pytest.raises(ValidationError):
        declarative.parse(extra)


def test_symlinks_are_not_followed_out_of_the_repository(tmp_path):
    outside = shard_repo(tmp_path / "outside")
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "v2").symlink_to(outside / "data" / "v2")
    groups, _ = declarative.discover(declarative.parse(SHARD_ADAPTER), repo)
    assert groups == []


# ---------------------------------------------------------------- repository-agnostic core


def test_one_catalog_handles_different_repository_structures_and_shared_adapters(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci"), "shards": shard_repo(tmp_path / "shards"),
                             "shards-copy": shard_repo(tmp_path / "shards-copy")})
    install(ctx, CASCI_ADAPTER, ["casci"])
    install(ctx, SHARD_ADAPTER, ["shards", "shards-copy"])  # one adapter, two repositories
    assert service.scan(ctx, "casci").by_status == {"verified": 2}
    for rid in ("shards", "shards-copy"):
        assert service.scan(ctx, rid).by_status == {"verified": 2}
    split = service.query(ctx.store, ArtifactQuery(kind="tabular-split", repository_id="shards",
                                                   filters={"split": ["train"]}))[0]
    assert split.member_count == 3 and split.metadata == {"schema": "tabular-v2", "shards": 3, "max_rows": 10}
    cond = service.query(ctx.store, ArtifactQuery(kind="toy-condition", filters={"k": [3]}))[0]
    assert cond.provenance.source == {"commit": "aaaaaaa1111", "config": "cfg-aaaaaaa1111"}
    assert cond.provenance.adapter_id == "toy-casci@1.0.0" and cond.provenance.rule == "condition"
    assert any(c.name.startswith("cross_reference") and c.passed and "matched" in c.detail for c in cond.checks)


def test_artifacts_without_declared_checks_are_never_verified(tmp_path):
    ctx = context(tmp_path, {"shards": shard_repo(tmp_path / "shards")})
    bare = copy.deepcopy(SHARD_ADAPTER)
    bare["name"] = "bare"
    bare["artifacts"][0]["checks"] = []
    install(ctx, bare, ["shards"])
    assert service.scan(ctx, "shards").by_status == {"unverified": 2}
    res = service.resolve(ctx, _q("catalog: kind=tabular-split split=train"))
    assert not res.ok and "unverified" in res.problems[0]


def test_inconsistent_content_is_detected_and_never_resolved(tmp_path):
    repo = casci_repo(tmp_path / "casci")
    target = next((repo / "cache").rglob("*-3.json"))
    doc = json.loads(target.read_text()); doc["energy"] += 1.0
    target.write_text(json.dumps(doc))
    ctx = context(tmp_path, {"casci": repo})
    install(ctx, CASCI_ADAPTER, ["casci"])
    assert service.scan(ctx, "casci").by_status == {"verified": 1, "inconsistent": 1}
    res = service.resolve(ctx, _q("catalog: kind=toy-condition molecule=H2 geometry_id=h2-r0.7 k=2..3"))
    assert [a.identity["k"] for a in res.artifacts] == [2]
    assert any("inconsistent" in p and "mismatched" in p for p in res.problems)


# ---------------------------------------------------------------- resolution


def test_same_condition_from_two_runs_is_ambiguous_until_provenance_is_pinned(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci", commits=("aaaaaaa1111", "bbbbbbb2222"))})
    install(ctx, CASCI_ADAPTER, ["casci"])
    assert service.scan(ctx, "casci").by_status == {"verified": 4}
    res = service.resolve(ctx, _q("catalog: kind=toy-condition repository=casci k=2..3 expect=2"))
    assert len(res.problems) == 2 and all("ambiguous" in p for p in res.problems) and not res.artifacts
    res = service.resolve(ctx, _q("catalog: kind=toy-condition repository=casci k=2..3 commit=bbbbbbb expect=2"))
    assert res.ok and {a.provenance["commit"] for a in res.artifacts} == {"bbbbbbb2222"}
    assert all(Path(f).is_file() for a in res.artifacts for f in a.files)


def test_missing_combinations_and_expected_count_fail_closed(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    install(ctx, CASCI_ADAPTER, ["casci"])
    service.scan(ctx, "casci")
    res = service.resolve(ctx, _q("catalog: kind=toy-condition molecule=H2 geometry_id=h2-r0.7 k=2..4"))
    assert res.problems == ["[catalog: kind=toy-condition molecule=H2 geometry_id=h2-r0.7 k=2..4] missing "
                            "{'molecule': 'H2', 'geometry_id': 'h2-r0.7', 'k': 4}"]
    res = service.resolve(ctx, _q("catalog: kind=toy-condition expect=3"))
    assert any("expected 3" in p for p in res.problems)
    assert not service.resolve(ctx, _q("catalog: kind=nothing")).ok


def test_content_changed_after_verification_becomes_stale(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    install(ctx, CASCI_ADAPTER, ["casci"])
    service.scan(ctx, "casci")
    target = next((tmp_path / "casci" / "results").rglob("cond_k2.json"))
    target.write_text(target.read_text().replace('"H2"', '"H2" '))
    res = service.resolve(ctx, _q("catalog: kind=toy-condition k=2"))
    assert not res.ok and "stale" in res.problems[0]
    stored = service.query(ctx.store, ArtifactQuery(kind="toy-condition", filters={"k": [2]}))[0]
    assert stored.status is ValidationStatus.STALE
    # A rescan registers the new bytes as a new artifact; the old one stays stale for audit.
    service.scan(ctx, "casci")
    kinds = [a["status"] for a in ctx.store.find(db.catalog_artifacts)]
    assert sorted(kinds) == ["stale", "verified", "verified"]


def test_removed_artifact_loses_its_location_on_rescan(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci")})
    install(ctx, CASCI_ADAPTER, ["casci"])
    service.scan(ctx, "casci")
    next((tmp_path / "casci" / "results").rglob("cond_k3.json")).unlink()
    service.scan(ctx, "casci")
    res = service.resolve(ctx, _q("catalog: kind=toy-condition k=3"))
    assert not res.ok and "stale" in res.problems[0]


def test_locations_are_tracked_per_environment(tmp_path):
    mac_root = casci_repo(tmp_path / "mac" / "casci")
    eagle_root = casci_repo(tmp_path / "eagle" / "casci")  # same bytes on another machine
    mac = context(tmp_path, {"casci": mac_root}, environment="mac")
    install(mac, CASCI_ADAPTER, ["casci"])
    service.scan(mac, "casci")
    eagle = service.CatalogContext(store=mac.store, registry=mac.registry, environment="eagle")
    res = service.resolve(eagle, _q("catalog: kind=toy-condition k=2"))
    assert not res.ok and "no location on environment 'eagle'" in res.problems[0]
    eagle.env_roots = {"eagle": {"casci": str(eagle_root)}}
    service.scan(eagle, "casci")
    art = service.query(mac.store, ArtifactQuery(kind="toy-condition", filters={"k": [2]}))[0]
    assert sorted(loc.environment for loc in art.locations) == ["eagle", "mac"]
    on_eagle = service.resolve(eagle, _q("catalog: kind=toy-condition k=2")).artifacts[0]
    on_mac = service.resolve(mac, _q("catalog: kind=toy-condition k=2")).artifacts[0]
    assert on_eagle.artifact_id == on_mac.artifact_id
    assert on_eagle.path.startswith(str(eagle_root.resolve())) and on_mac.path.startswith(str(mac_root.resolve()))


def test_registry_locations_name_other_environments(tmp_path):
    from research_autopilot.implementation.models import RepoConfig
    ctx = context(tmp_path, {})
    ctx.registry["r"] = RepoConfig(id="r", path="/here", locations={"eagle": "~/there"})
    assert ctx.root("r") == Path("/here")
    assert ctx.root("r", "eagle") == Path("~/there").expanduser()
    assert ctx.root("r", "tsubame") is None


def test_requirement_parsing():
    qs = service.parse_requirements(
        "Use the cached landscape.\n- catalog: kind=x repository=r molecule=N2,BeO k=2..4 commit=6c0d1dd expect=6\n"
        "* `catalog: kind=y`")
    assert qs[0].filters == {"molecule": ["N2", "BeO"], "k": [2, 3, 4], "commit": ["6c0d1dd"]}
    assert (qs[0].repository_id, qs[0].expect_count, qs[1].kind) == ("r", 6, "y")
    with pytest.raises(ValueError):
        service.parse_requirements("catalog: molecule=N2")
