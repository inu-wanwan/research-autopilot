"""Phase 7B: autonomous adapter generation, validation and policy-gated activation."""

import copy
import json

import pytest
from catalog_fakes import (CASCI_ADAPTER, HUMAN, RUNS_ADAPTER, SHARD_ADAPTER, casci_repo, context, install, runs_repo,
                           shard_repo)

from research_autopilot.catalog import adapters, generation, sandbox, service
from research_autopilot.catalog.generation import AdapterProposal, GeneratorRun
from research_autopilot.catalog.models import AdapterOrigin, AdapterState
from research_autopilot.storage import db

needs_sandbox = pytest.mark.skipif(not sandbox.available(), reason="no OS sandbox on this host")


class FakeGenerator:
    model = "fake-generator"

    def __init__(self, *proposals):
        self.proposals = list(proposals)
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(payload)
        p = self.proposals.pop(0)
        if isinstance(p, Exception):
            raise p
        return GeneratorRun(output=p, model=self.model, cost_usd=0.01)


class FakeNotifier:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)
        return True


def decl(definition, **kw):
    body = {k: v for k, v in definition.items() if k not in ("name", "version")}
    return AdapterProposal(kind="declarative", definition_json=json.dumps(body), python_source="",
                           rationale=kw.get("rationale", "fits the survey"), assumptions=[], unresolved=[])


def py(source):
    return AdapterProposal(kind="python", definition_json="", python_source=source, rationale="needs code",
                           assumptions=[], unresolved=[])


# ---------------------------------------------------------------- detection


def test_detection_reports_why_a_repository_needs_an_adapter(tmp_path):
    ctx = context(tmp_path, {"casci": casci_repo(tmp_path / "casci"), "gone": tmp_path / "missing"})
    assert generation.detect(ctx, "gone").reason == "repository_unavailable"
    assert generation.detect(ctx, "casci").reason == "unbound"
    rec = adapters.register(ctx.store, kind=adapters.AdapterKind.DECLARATIVE, origin=AdapterOrigin.MANUAL,
                            actor=HUMAN, definition=CASCI_ADAPTER)
    adapters.bind(ctx.store, "casci", ["toy-casci"], actor=HUMAN, registry=ctx.registry)
    assert generation.detect(ctx, "casci").reason == "no_active_adapter"
    adapters.validate(ctx.store, rec.adapter_id, repository_id="casci", root=ctx.root("casci"), actor=HUMAN)
    adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)
    service.scan(ctx, "casci")
    assert generation.detect(ctx, "casci").needed is False
    next((tmp_path / "casci" / "results").rglob("cond_k2.json")).unlink()
    service.scan(ctx, "casci")
    assert generation.detect(ctx, "casci").reason == "stale_artifacts"


# ---------------------------------------------------------------- declarative: auto activation


@pytest.mark.parametrize("make_repo,definition,kind", [
    (casci_repo, CASCI_ADAPTER, "toy-condition"),
    (shard_repo, SHARD_ADAPTER, "tabular-split"),
    (runs_repo, RUNS_ADAPTER, "training-metrics"),
])
def test_generated_declarative_adapters_are_validated_and_auto_activated_for_different_structures(
        tmp_path, make_repo, definition, kind):
    ctx = context(tmp_path, {"repo": make_repo(tmp_path / "repo")})
    notifier = FakeNotifier()
    gen = FakeGenerator(decl(definition))
    run = generation.generate_adapter(ctx, "repo", gen, focus="all results", notifier=notifier)
    assert run.outcome == "activated" and run.adapter_id == "repo.generated@0.1.0"
    rec = adapters.get(ctx.store, run.adapter_id)
    assert rec.origin is AdapterOrigin.GENERATED and rec.state is AdapterState.ACTIVE
    assert rec.activated_by == adapters.AUTOPILOT_ACTOR and rec.generator["model"] == "fake-generator"
    assert rec.generator["survey_hash"] == run.survey_hash and rec.validation.passed
    assert {c.name for c in rec.validation.checks} >= {"definition_safety", "deterministic", "repository_unchanged",
                                                       "verifies_some_artifacts"}
    scan = service.scan(ctx, "repo")
    assert scan.discovered and set(scan.by_status) == {"verified"}
    assert service.query(ctx.store, service.ArtifactQuery(kind=kind))
    assert gen.payloads[0]["SURVEY"]["path_shapes"] and gen.payloads[0]["FOCUS"] == "all results"
    assert [e.details[1] for e in notifier.events] == ["Outcome: activated"]
    assert generation.history(ctx.store, "repo")[0].outcome == "activated"
    assert generation.detect(ctx, "repo").needed is False


def test_invalid_proposals_get_feedback_and_never_activate(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    escape = copy.deepcopy(CASCI_ADAPTER)
    escape["artifacts"][0]["pattern"] = "../other/{x}.json"
    unchecked = copy.deepcopy(CASCI_ADAPTER)
    unchecked["artifacts"][0]["checks"] = []
    gen = FakeGenerator(decl(escape), decl(unchecked), decl(CASCI_ADAPTER))
    run = generation.generate_adapter(ctx, "repo", gen)
    assert run.outcome == "activated" and len(run.attempts) == 3
    assert "pattern" in run.attempts[0].problems[0]
    assert any("declares no mandatory check" in p for p in run.attempts[1].problems)
    assert gen.payloads[1]["PREVIOUS_ATTEMPT_PROBLEMS"] == run.attempts[0].problems
    states = {r["adapter_id"]: r["state"] for r in ctx.store.find(db.catalog_adapters)}
    assert states == {"repo.generated@0.1.0": "rejected", "repo.generated@0.2.0": "rejected",
                      "repo.generated@0.3.0": "active"}


def test_proposal_whose_checks_reject_the_real_data_is_rejected(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    wrong = copy.deepcopy(CASCI_ADAPTER)
    wrong["artifacts"][0]["checks"].append({"type": "equals", "field": "molecule", "value": "CO2"})
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(wrong)), max_attempts=1)
    assert run.outcome == "rejected" and "verifies_some_artifacts" in run.attempts[0].problems[0]
    assert adapters.active_for(ctx.store, "repo") == []
    assert ctx.store.get(db.catalog_bindings, "repo") is None


def test_generator_failure_or_budget_refusal_leaves_everything_unchanged(tmp_path):
    from research_autopilot.experiments.analyst import LlmBudgetError
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(LlmBudgetError("no price configured")))
    assert run.outcome == "failed" and "no price configured" in run.note
    assert ctx.store.find(db.catalog_adapters) == []


def test_failed_regeneration_never_overwrites_the_active_generated_adapter(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    first = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(CASCI_ADAPTER)))
    service.scan(ctx, "repo")
    broken = copy.deepcopy(CASCI_ADAPTER)
    broken["artifacts"][0]["pattern"] = "/abs/{x}.json"
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(broken)), force=True, max_attempts=1)
    assert run.outcome == "rejected"
    assert [a.adapter_id for a in adapters.active_for(ctx.store, "repo")] == [first.adapter_id]


def test_regression_needs_a_human_and_keeps_the_current_version_active(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    first = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(CASCI_ADAPTER)))
    service.scan(ctx, "repo")
    narrower = copy.deepcopy(CASCI_ADAPTER)
    narrower["artifacts"][0]["pattern"] = "results/run_{run}/{geometry_id}/cond_k2.json"
    narrower["artifacts"][0]["checks"] = [c for c in narrower["artifacts"][0]["checks"] if c.get("field") != "file_k"]
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(narrower)), force=True)
    assert run.outcome == "awaiting_human" and "regression" in run.note
    assert adapters.get(ctx.store, run.adapter_id).state is AdapterState.VALIDATED
    assert [a.adapter_id for a in adapters.active_for(ctx.store, "repo")] == [first.adapter_id]


def test_generated_adapter_never_displaces_a_human_made_one(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    install(ctx, CASCI_ADAPTER, ["repo"])
    service.scan(ctx, "repo")
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(CASCI_ADAPTER)), force=True)
    assert run.outcome == "awaiting_human" and "human-made" in run.note
    assert [a.adapter_id for a in adapters.active_for(ctx.store, "repo")] == ["toy-casci@1.0.0"]


def test_no_generation_when_not_needed_unless_forced(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    install(ctx, CASCI_ADAPTER, ["repo"])
    service.scan(ctx, "repo")
    gen = FakeGenerator()
    assert generation.generate_adapter(ctx, "repo", gen).outcome == "skipped" and gen.payloads == []


def test_survey_is_bounded_and_skips_secrets(tmp_path):
    from research_autopilot.catalog.survey import survey
    repo = runs_repo(tmp_path / "repo")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-should-never-leak")
    (repo / "experiments" / "api_token.json").write_text('{"token": "sk-should-never-leak"}')
    s = survey(repo, focus="metrics")
    text = json.dumps(s)
    assert "sk-should-never-leak" not in text and "api_token" not in text
    assert s["path_shapes"][0]["shape"].endswith("metrics.json")
    assert s["path_shapes"][0]["json_structure"]["loss"] == 0.1


# ---------------------------------------------------------------- python adapters


GOOD_PY = '''
import json, os

def discover(root):
    out = []
    base = os.path.join(root, "experiments")
    for exp in sorted(os.listdir(base)):
        for seed_dir in sorted(os.listdir(os.path.join(base, exp))):
            rel = f"experiments/{exp}/{seed_dir}/metrics.json"
            with open(os.path.join(root, rel)) as fh:
                doc = json.load(fh)
            out.append({"rule": "metrics", "kind": "training-metrics",
                        "identity": {"experiment": exp, "seed": doc["seed"]},
                        "metadata": {"loss": doc["loss"]}, "provenance": {}, "relative_dir": os.path.dirname(rel),
                        "members": [rel],
                        "checks": [{"name": "experiment_matches", "passed": doc["experiment"] == exp}]})
    return out
'''


@pytest.mark.parametrize("source,expected", [
    ("import subprocess\ndef discover(root):\n    return []\n", "import subprocess is not allowed"),
    ("def discover(root):\n    return eval('[]')\n", "call to eval() is not allowed"),
    ("def discover(root):\n    open(root + '/x', 'w')\n    return []\n", "write mode"),
    ("import os\ndef discover(root):\n    os.remove(root)\n    return []\n", ".remove()"),
    ("def find(root):\n    return []\n", "must define discover"),
])
def test_python_static_gate(source, expected):
    assert any(expected in p for p in generation.python_source_problems(source))


@needs_sandbox
def test_generated_python_adapter_is_sandbox_validated_and_waits_for_a_human(tmp_path):
    ctx = context(tmp_path, {"repo": runs_repo(tmp_path / "repo")})
    notifier = FakeNotifier()
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(py(GOOD_PY)), notifier=notifier)
    assert run.outcome == "awaiting_human", run.attempts[-1].problems
    rec = adapters.get(ctx.store, run.adapter_id)
    assert rec.state is AdapterState.VALIDATED and rec.python_source and rec.entrypoint is None
    assert any("adapter activate" in d for d in notifier.events[0].details)
    adapters.bind(ctx.store, "repo", [rec.name], actor=HUMAN, registry=ctx.registry)
    assert service.scan(ctx, "repo").discovered == 0  # not active -> never used
    with pytest.raises(adapters.AdapterError, match="human"):
        adapters.activate(ctx.store, rec.adapter_id, actor=adapters.AUTOPILOT_ACTOR)
    adapters.activate(ctx.store, rec.adapter_id, actor=HUMAN)
    scan = service.scan(ctx, "repo")  # runs in the sandbox, members re-hashed by the parent
    assert scan.by_status == {"verified": 4}


@needs_sandbox
def test_sandbox_blocks_writes_network_processes_and_reads_outside_the_repository(tmp_path):
    repo = runs_repo(tmp_path / "repo")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("secret")
    probe = f'''
import os, socket, subprocess, json
def attempt(fn):
    try:
        fn()
        return "allowed"
    except Exception as exc:
        return type(exc).__name__
def discover(root):
    result = {{
        "write_repo": attempt(lambda: open(os.path.join(root, "pwned.txt"), "w").write("x")),
        "read_outside": attempt(lambda: open({str(outside)!r}).read()),
        "network": attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=2)),
        "subprocess": attempt(lambda: subprocess.run(["/bin/echo", "x"], check=True)),
        "read_repo": attempt(lambda: open(os.path.join(root, "experiments/baseline/seed_0/metrics.json")).read()),
    }}
    return [{{"rule": "probe", "kind": "probe", "identity": result, "members": ["experiments/baseline/seed_0/metrics.json"]}}]
'''
    raw, err = sandbox.run_python_adapter(probe, repo, timeout=30)  # bypasses the static gate on purpose
    assert err is None
    seen = raw[0]["identity"]
    assert seen["read_repo"] == "allowed"
    assert all(seen[k] != "allowed" for k in ("write_repo", "read_outside", "network", "subprocess")), seen
    assert not (repo / "pwned.txt").exists()


def test_untrusted_python_output_is_checked_and_rehashed(tmp_path):
    repo = runs_repo(tmp_path / "repo")
    raw = [{"rule": "r", "kind": "k", "identity": {"a": 1}, "members": ["../outside.json"]},
           {"rule": "r", "kind": "k", "identity": {"a": 2}, "members": ["experiments/baseline/seed_0/metrics.json"],
            "checks": [{"name": "c", "passed": True}]}]
    groups, errors = sandbox.to_groups(raw, repo)
    assert len(groups) == 1 and "not a regular file inside the repository" in errors[0]
    assert len(groups[0].members["experiments/baseline/seed_0/metrics.json"]) == 64
