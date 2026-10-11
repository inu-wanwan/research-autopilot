"""Audit fixes: Definition of Done quote matching and replan accounting, research scope number / budget /
autonomy verification, the Phase 4 environment contract, and environment failure classification."""

import sys
import time

from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review
from research_fakes import RESEARCH_SECTIONS, scope
from research_autopilot.experiments import orchestrator as exp_orch
from research_autopilot.experiments.environment import EnvironmentError_, resolve_argv
from research_autopilot.implementation import facts as facts_mod
from research_autopilot.implementation.prompts import IMPLEMENTER_RULES, REVIEWER_RULES
from research_autopilot.models import AcceptanceMapping, DoneCriterion, TaskSpec
from research_autopilot.orchestrator.core import plan_task
from research_autopilot.planning import dod
from research_autopilot.research import scope as scope_mod
from research_autopilot.research.models import Autonomy, BudgetItem, ConditionBound


def spec(**kw) -> TaskSpec:
    base = dict(task_id="INU-1", title="t", category=None, purpose="Compute the H2 curve.", background=None,
                scientific_rationale=None, specification="- Record **rhf.conv_tol** for every result.\n"
                                                         "- Write `results.json` with one row per geometry.",
                acceptance_criteria="1. Energy error table for all geometries", dependencies_note=None,
                scientific_invariants="- same basis", artifact_requirements=None, other_sections={}, preamble=None,
                sources=[])
    return TaskSpec.model_construct(**(base | kw))


def plan_with(source: str):
    return make_plan().model_copy(update={
        "definition_of_done": [DoneCriterion(id="D1", statement="Each result records rhf.conv_tol.", source=source,
                                             check="test", how="test")],
        "acceptance_mapping": [AcceptanceMapping(criterion="Energy error table for all geometries",
                                                 covered_by_steps=["S2"], verification="x", done_criteria=["D1"])]})


# ---------------------------------------------------------------- 1. DoD quotes


def test_dod_quotes_ignore_markdown_quote_marks_bullets_and_width():
    for source in ("Record rhf.conv_tol for every result. Write results.json with one row per geometry",
                   "「Record rhf.conv_tol for every result」", "“Write `results.json` with one row”",
                   "Ｒｅｃｏｒｄ rhf.conv_tol for every result", "1. Energy error table for all geometries"):
        assert dod.problems(spec(), plan_with(source)) == [], source


def test_dod_quotes_may_come_from_any_spec_section_but_invented_ones_are_rejected():
    s = spec(background="Earlier runs lacked convergence flags.", other_sections={"Notes": "Use the 9 geometries."},
             file_scope="src/h2/**", dependencies_note="needs INU-0 output")
    for source in ("Earlier runs lacked convergence flags", "Use the 9 geometries", "needs INU-0 output"):
        assert dod.problems(s, plan_with(source)) == [], source
    assert any("quote the task specification" in p for p in dod.problems(s, plan_with("Use the 12 geometries")))
    assert dod.problems(s, plan_with("phase4_contract")) == []


# ---------------------------------------------------------------- 2. DoD rejections and replans


def _plan_deps(tmp_path, planner, reviewer, **kw):
    return make_deps(tmp_path, FakeNotion(make_page()), FakeLinear(make_issue()), planner, reviewer, **kw)


def _bad():
    p = make_plan()
    return p.model_copy(update={"definition_of_done": [], "acceptance_mapping": [
        m.model_copy(update={"done_criteria": []}) for m in p.acceptance_mapping]})


def test_a_dod_rejection_does_not_consume_a_replan(tmp_path):
    deps = _plan_deps(tmp_path, [_bad(), make_plan()], [review("approved")], max_replans=0)
    assert plan_task(deps, task_id="INU-900", mode="dry_run").decision.status.value == "ready"


def test_dod_feedback_keeps_the_reviewers_requests(tmp_path):
    deps = _plan_deps(tmp_path, [make_plan(), _bad(), make_plan()],
                      [review("revision_required"), review("approved")], max_replans=1)
    assert plan_task(deps, task_id="INU-900", mode="dry_run").decision.status.value == "ready"
    issues = [r.issue for r in deps.planner.calls[2][2]]
    assert "metric unclear" in issues and any(i.startswith("Definition of Done:") for i in issues)


def test_repeated_dod_rejections_end_the_run_with_a_dod_message(tmp_path):
    deps = _plan_deps(tmp_path, [_bad()] * (dod.MAX_DOD_REJECTIONS + 1), [])
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status.value == "blocked" and deps.reviewer.calls == []
    assert len(deps.planner.calls) == dod.MAX_DOD_REJECTIONS + 1
    msg = rec.decision.reasons[0].message
    assert "Definition of Done was not acceptable" in msg and "replans" not in msg


# ---------------------------------------------------------------- 3. numbers


def test_numbers_match_whole_values_only():
    assert not scope_mod._num_ok(1, "最大10点")
    assert not scope_mod._num_ok(5, "up to 15 geometries")
    assert not scope_mod._num_ok(0.5, "R = 10.5 Å")
    assert scope_mod._num_ok(10, "最大10点") and scope_mod._num_ok(0.5, "0.50 Å")
    assert scope_mod._num_ok(1000, "at most 1,000 samples") and scope_mod._num_ok(12, "最大１２点")
    assert scope_mod._numbers("1, 2, 3") == {"1", "2", "3"}


def _spec(**sections):
    return TaskSpec.from_sources(make_issue(), make_page(sections=dict(RESEARCH_SECTIONS, **sections)))


def test_ranges_with_units_and_numeric_values_are_checked():
    s = _spec(specification="bond length R from 0.5 Å – 3.0 Å; basis sets 1, 2 and 3 zeta; R=0.5Å〜3.0Å here")
    for q in ("bond length R from 0.5 Å – 3.0 Å", "R=0.5Å〜3.0Å here"):
        c = scope_mod.verify(scope(conditions=[ConditionBound(name="r", kind="range", quote=q)]), s).conditions[0]
        assert (c.minimum, c.maximum) == (0.5, 3.0), q
    ok = ConditionBound(name="zeta", kind="values", values=["1", "2", "3"], quote="basis sets 1, 2 and 3 zeta")
    bad = ConditionBound(name="zeta", kind="values", values=["1", "4"], quote="basis sets 1, 2 and 3 zeta")
    assert scope_mod.verify(scope(conditions=[ok]), s).conditions[0].status == "stated"
    out = scope_mod.verify(scope(conditions=[bad]), s)
    assert out.conditions[0].status == "ambiguous" and any(q.field == "conditions.zeta" for q in out.blocking())


# ---------------------------------------------------------------- 4. budget per field


def test_each_budget_limit_is_verified_with_its_own_quote():
    s = _spec(budget="- Compute: 1 CPU-hour in total\n- API: 5 USD")
    ok = scope(budget=BudgetItem(cpu_hours=1, api_usd=5, cpu_hours_quote="Compute: 1 CPU-hour in total",
                                 api_usd_quote="5 USD", status="stated"))  # short quote, in the budget section
    b = scope_mod.verify(ok, s).budget
    assert b.status == "stated" and (b.cpu_hours, b.api_usd) == (1, 5)
    wrong = scope(budget=BudgetItem(cpu_hours=1, api_usd=50, cpu_hours_quote="Compute: 1 CPU-hour in total",
                                    api_usd_quote="5 USD", status="stated"))
    out = scope_mod.verify(wrong, s)
    assert out.budget.status == "ambiguous" and out.budget.api_usd == 50  # kept for the human, not defaulted
    assert any("api_usd" in q.question and q.blocking for q in out.open_questions)
    assert not any("cpu_hours" in q.question for q in out.open_questions)
    elsewhere = scope(budget=BudgetItem(cpu_hours=1, api_usd=5, cpu_hours_quote="Compute: 1 CPU-hour in total",
                                        api_usd_quote="5 USD", status="stated"))
    s2 = _spec(budget="- Compute: 1 CPU-hour in total", purpose="We spent 5 USD before.")
    assert scope_mod.verify(elsewhere, s2).budget.status == "ambiguous"  # short quote outside the budget section


def test_budget_falls_back_to_the_shared_quote():
    s = _spec()
    b = scope(budget=BudgetItem(cpu_hours=1, api_usd=5, quote="Compute budget: 1 CPU-hour in total; API budget 5 USD",
                                status="stated"))
    assert scope_mod.verify(b, s).budget.status == "stated"


# ---------------------------------------------------------------- 5. autonomy


def test_each_autonomy_limit_must_be_tied_to_its_own_phrase():
    quote = "Autonomy: at most 4 cycles; 2 retries per task; stop after 3 cycles without progress"
    s = _spec(purpose=quote)
    a = scope_mod.verify(scope(autonomy=Autonomy(max_cycles=4, max_new_tasks_per_cycle=1, max_retries_per_task=2,
                                                 no_progress_cycles=3, status="stated", quote=quote)), s).autonomy
    assert (a.max_cycles, a.max_retries_per_task, a.no_progress_cycles) == (4, 2, 3)
    assert a.max_new_tasks_per_cycle == Autonomy().max_new_tasks_per_cycle  # no number for tasks per cycle
    swapped = scope(autonomy=Autonomy(max_cycles=2, max_new_tasks_per_cycle=4, max_retries_per_task=3,
                                      no_progress_cycles=4, status="stated", quote=quote))
    a = scope_mod.verify(swapped, s).autonomy
    d = Autonomy()
    assert (a.max_cycles, a.max_new_tasks_per_cycle, a.max_retries_per_task, a.no_progress_cycles) == \
        (d.max_cycles, d.max_new_tasks_per_cycle, d.max_retries_per_task, d.no_progress_cycles)


def test_japanese_autonomy_limits_are_tied_to_their_phrases():
    nums = scope_mod._limit_numbers("最大5サイクル、1サイクルあたり3タスク、再試行は2回まで、進展のないサイクルが2回続いたら停止")
    assert nums == {"max_cycles": {"5"}, "max_new_tasks_per_cycle": {"3"}, "max_retries_per_task": {"2"},
                    "no_progress_cycles": {"2"}}


def test_allowed_actions_are_ra_defaults_unless_quoted():
    quote = "Autonomy: at most 4 cycles. Allowed: auxiliary analyses"
    s = _spec(purpose=quote)
    invented = scope(autonomy=Autonomy(max_cycles=4, status="stated", quote=quote,
                                       allowed_actions=["change the basis set"]))
    assert scope_mod.verify(invented, s).autonomy.allowed_actions == Autonomy().allowed_actions
    quoted = scope(autonomy=Autonomy(max_cycles=4, status="stated", quote=quote, allowed_actions=["auxiliary analyses"]))
    assert scope_mod.verify(quoted, s).autonomy.allowed_actions == ["auxiliary analyses"]


# ---------------------------------------------------------------- 6. environment contract


def test_the_environment_contract_says_the_repository_is_not_installed():
    for text in (facts_mod.PHASE4_ENVIRONMENT_CONTRACT, IMPLEMENTER_RULES, REVIEWER_RULES):
        assert "--no-install-project" in text and "NOT" in text and "working_directory" in text
        assert "src/" in text and "setup" in text


def test_test_environment_packages_are_not_cached_forever(monkeypatch):
    from research_autopilot.implementation.models import RepoConfig
    cfg = RepoConfig(id="r", path=".", base_branch="main", test_command=[sys.executable, "-m", "pytest"])
    monkeypatch.setitem(facts_mod._cache, sys.executable, (time.monotonic(), ["fresh==1"]))
    assert facts_mod._test_packages(cfg) == ["fresh==1"]
    monkeypatch.setitem(facts_mod._cache, sys.executable,
                        (time.monotonic() - facts_mod.CACHE_TTL_SECONDS - 1, ["stale==1"]))
    assert facts_mod._test_packages(cfg) != ["stale==1"]


# ---------------------------------------------------------------- 7. environment failure classification


def _kind(message: str) -> str:
    return exp_orch.SETUP_DEFECT if exp_orch._DETERMINISTIC_ENV.search(message) else exp_orch.ENVIRONMENT_FAILURE


def test_deterministic_environment_failures_are_not_reported_as_transient():
    try:
        resolve_argv(["bash", "run.sh"], "/venv/bin/python")
    except EnvironmentError_ as exc:
        launcher = str(exc)
    deterministic = [launcher, "uv.lock not found at 0123456789ab", "unsupported dependency files ['setup.cfg']",
                     "setup step ['python', 'prep.py'] failed (exit 1): Traceback",
                     "uv pip install failed (1): × No solution found when resolving dependencies",
                     "Because pyscf==99 was not found in the package registry"]
    for msg in deterministic:
        assert _kind(msg) == exp_orch.SETUP_DEFECT, msg
    transient = ["uv venv --quiet failed (2): error: Failed to download https://github.com/... python",
                 "uv pip install failed (2): Could not connect to pypi.org: Connection reset",
                 "uv failed: Command timed out after 1800 seconds", "timed out waiting for environment abc",
                 "setup step ['python', 'prep.py'] timed out"]
    for msg in transient:
        assert _kind(msg) == exp_orch.ENVIRONMENT_FAILURE, msg
    assert exp_orch.ENVIRONMENT_FAILURE not in exp_orch.SETUP_DEFECT


def test_units_may_be_declared_for_numeric_lists_objects_and_flags():
    from research_autopilot.experiments.validator import _quantity
    assert _quantity([0.5, 1.0, 3.0]) and _quantity({"atom": "H", "x": 0.0, "z": 0.5}) and _quantity(True)
    assert _quantity([{"atom": "H", "x": 0.0}, {"atom": "H", "x": 0.74}])
    assert not _quantity("Angstrom") and not _quantity([]) and not _quantity(float("nan"))
    assert not _quantity([1.0, float("inf")]) and not _quantity({"atom": "H"})


def test_container_repositories_run_experiments_in_their_own_image(tmp_path):
    from research_autopilot.experiments.environment import container_argv
    from research_autopilot.implementation.facts import container_contract
    from research_autopilot.implementation.models import RepoConfig
    c = {"compose_project": "proj", "service": "research", "workdir": "/workspace"}
    argv = container_argv(c, tmp_path / "src", ["uv", "run", "python", "run.py", "--n", "3"], "exp",
                          {"RA_CATALOG_INPUTS": "/jobs/1/inputs.json"}, [tmp_path / "job", tmp_path / "job"])
    assert argv[:2] == ["docker", "compose"] and "--project-directory" in argv and str(tmp_path / "src") in argv
    assert argv[argv.index("-w") + 1] == "/workspace/exp" and argv[-5:] == ["research", "python", "run.py", "--n", "3"]
    assert argv.count("-v") == 1 and "RA_CATALOG_INPUTS=/jobs/1/inputs.json" in argv  # mounts deduplicated
    repo = RepoConfig(id="r", path=str(tmp_path), container=c)
    assert "docker compose run research" in container_contract(repo)
