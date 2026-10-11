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
    assert gen.payloads[1]["PREVIOUS_ATTEMPT_PROBLEMS"] == [f"(latest attempt) {p}" for p in run.attempts[0].problems]
    assert gen.payloads[1]["PREVIOUS_ATTEMPT_PROPOSAL"]["kind"] == "declarative"
    # Earlier lessons are kept for the third attempt.
    assert any(p.startswith("(earlier attempt)") and "pattern" in p for p in gen.payloads[2]["PREVIOUS_ATTEMPT_PROBLEMS"])
    states = {r["adapter_id"]: r["state"] for r in ctx.store.find(db.catalog_adapters)}
    assert states == {"repo.generated@0.1.0": "rejected", "repo.generated@0.2.0": "rejected",
                      "repo.generated@0.3.0": "active"}


def test_proposal_whose_checks_reject_the_real_data_is_rejected(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    wrong = copy.deepcopy(CASCI_ADAPTER)
    wrong["artifacts"][0]["checks"].append({"type": "equals", "field": "molecule", "value": "CO2"})
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(wrong)), max_attempts=1)
    assert run.outcome == "rejected"
    assert any("verifies_some_artifacts" in p and "equals:molecule" in p for p in run.attempts[0].problems)
    assert any(p.startswith("diagnostics:") for p in run.attempts[0].problems)
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
    assert notifier.events[0].needs_human and any("adapter activate" in s for s in notifier.events[0].steps)
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


def test_empty_pointer_addresses_the_whole_list_element_and_field_capture_clash_is_explained(tmp_path):
    from pydantic import ValidationError

    from research_autopilot.catalog import declarative
    repo = shard_repo(tmp_path / "repo")
    d = copy.deepcopy(SHARD_ADAPTER)
    d["artifacts"][0]["checks"].append({"type": "items_finite", "field": "ids", "item": ""})
    groups, _ = declarative.discover(declarative.parse(d), repo)
    assert all(c.passed for g in groups for c in g.checks)
    d["artifacts"][0]["fields"]["split"] = "/split"
    with pytest.raises(ValidationError, match="shadow path captures"):
        declarative.parse(d)


def test_generated_adapter_artifacts_are_reused_by_default_and_human_trust_is_opt_in(tmp_path):
    ctx = context(tmp_path, {"repo": casci_repo(tmp_path / "repo")})
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(CASCI_ADAPTER)))
    service.scan(ctx, "repo")
    q = "catalog: kind=toy-condition k=2..3 expect=2"
    assert service.resolve(ctx, service.parse_requirements(q)).ok  # default: generated checks are enough
    strict = service.resolve(ctx, service.parse_requirements(q + " trust=human"))
    assert not strict.artifacts and any("no human has reviewed" in p for p in strict.problems)
    with pytest.raises(adapters.AdapterError, match="human"):
        adapters.endorse(ctx.store, run.adapter_id, actor=adapters.AUTOPILOT_ACTOR)
    adapters.endorse(ctx.store, run.adapter_id, actor=HUMAN)
    assert service.resolve(ctx, service.parse_requirements(q + " trust=human")).ok


def test_too_fine_artifact_granularity_is_rejected_with_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "MAX_ARTIFACTS", 3)
    ctx = context(tmp_path, {"repo": shard_repo(tmp_path / "repo")})
    per_file = copy.deepcopy(SHARD_ADAPTER)
    per_file["artifacts"][0]["identity"] = ["version", "split", "index"]
    per_file["artifacts"][0]["checks"] = [{"type": "equals", "field": "schema", "value": "tabular-v2"}]
    run = generation.generate_adapter(ctx, "repo", FakeGenerator(decl(per_file)), max_attempts=1)
    assert run.outcome == "rejected"
    assert any("artifact_granularity" in p and "group member files" in p for p in run.attempts[0].problems)



# ---------------------------------------------------------------- autonomous upkeep from the Planner


REQ = "- catalog: kind=toy-condition repository=repo molecule=H2 geometry_id=h2-r0.7 k=2..3 expect=2"


def _planning(tmp_path, gen, repo_builder=casci_repo):
    from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review

    from research_autopilot.implementation.models import RepoConfig
    page = make_page()
    page = page.model_copy(update={"sections": {**page.sections, "input_artifacts": REQ}})
    deps = make_deps(tmp_path, FakeNotion(page), FakeLinear(make_issue()), [make_plan()] * 3, [review("approved")] * 3)
    repo = repo_builder(tmp_path / "repo")
    deps.store.init_schema()
    deps.catalog = service.CatalogContext(store=deps.store, registry={"repo": RepoConfig(id="repo", path=str(repo))},
                                          environment="mac", generator_factory=lambda: gen, notifier=FakeNotifier())
    return deps, repo


def test_planner_generates_activates_scans_and_resolves_without_any_human_step(tmp_path):
    from research_autopilot.models import DecisionStatus
    from research_autopilot.orchestrator.core import plan_task
    gen = FakeGenerator(py(GOOD_PY), decl(CASCI_ADAPTER))  # python first: sent back for a declarative rewrite
    deps, _ = _planning(tmp_path, gen)
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status is DecisionStatus.READY, [r.message for r in rec.decision.reasons]
    run = generation.history(deps.store, "repo")[0]
    assert run.outcome == "activated" and "cannot be adopted autonomously" in run.attempts[0].problems[0]
    assert adapters.active_for(deps.store, "repo")[0].origin is AdapterOrigin.GENERATED
    assert "Resolved catalog artifacts (2" in deps.planner.calls[0][0].input_artifacts
    # A second task finds everything ready: no generation, no rescan.
    plan_task(deps, task_id="INU-900", mode="dry_run")
    assert len(gen.payloads) == 2 and len(deps.store.find(db.catalog_scans)) == 1


def test_failed_autonomous_generation_blocks_the_task_and_is_not_retried_for_the_same_commit(tmp_path):
    from research_autopilot.models import DecisionStatus
    from research_autopilot.orchestrator.core import plan_task
    wrong = copy.deepcopy(CASCI_ADAPTER)
    wrong["artifacts"][0]["pattern"] = "nothing/{x}/*.json"
    wrong["artifacts"][0].update(identity=["x"], provenance=[], metadata=[], captures={},
                                 checks=[{"type": "equals", "field": "x", "value": "y"}])
    gen = FakeGenerator(*[decl(wrong)] * 3)
    deps, _ = _planning(tmp_path, gen)
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status is DecisionStatus.BLOCKED
    assert any("adapter generation rejected" in r.message for r in rec.decision.reasons)
    assert deps.planner.calls == [] and len(gen.payloads) == 3
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert any("not retried" in r.message for r in rec.decision.reasons) and len(gen.payloads) == 3


def test_new_repository_commit_triggers_a_rescan(tmp_path):
    import subprocess

    from research_autopilot.catalog.integration import ensure_repository

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    gen = FakeGenerator(decl(CASCI_ADAPTER))
    deps, repo = _planning(tmp_path, gen)
    git("init", "-q"); git("add", "."); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
    ensure_repository(deps.catalog, "repo")
    assert ensure_repository(deps.catalog, "repo") == []  # nothing changed: no work
    casci_repo(repo, conditions=(("H2", "h2-r0.7", 4),))
    git("add", "."); git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
    notes = ensure_repository(deps.catalog, "repo")
    assert any("rescanned" in n for n in notes)
    assert service.resolve(deps.catalog, service.parse_requirements("catalog: kind=toy-condition k=4")).ok
