"""Phase 9: trusted onboarding of external repositories (offline: mock GitHub, scripted Docker and planner)."""

from pathlib import Path

import pytest
from external_fakes import FILES, GPL, SHA1, SHA2, FakeDocker, GitHub, Planner

from research_autopilot.catalog.service import ArtifactQuery, CatalogContext, query
from research_autopilot.external import deps as deps_mod
from research_autopilot.external import github, grants, integration, registry, service
from research_autopilot.external import license as lic
from research_autopilot.external.models import (EntryState, ExecutionRequest, ExecutionScope, FileAccess,
                                                GrantStatus, PaperRef, Provider)
from research_autopilot.notifications import LoggingNotifier
from research_autopilot.storage import db
from research_autopilot.storage.db import Store

PAPER = PaperRef(title="Bayesian Optimization of Combinatorial Structures", arxiv_id="1806.08838",
                 authors=["Ricardo Baptista", "Matthias Poloczek"])


@pytest.fixture(autouse=True)
def ext_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RA_EXTERNAL_DIR", str(tmp_path / "external"))
    return tmp_path / "external"


def _deps(tmp_path, gh=None, docker=None, llm="default"):
    store = Store(f"sqlite:///{tmp_path / 'ra.db'}")
    store.init_schema()
    catalog = registry.with_external(CatalogContext(store=store, registry={}, environment="mac"))
    return service.ExternalDeps(store=store, net=(gh or GitHub()).net(), docker=docker or FakeDocker(),
                                llm=Planner() if llm == "default" else llm, catalog=catalog,
                                notifier=LoggingNotifier())


def _decide(store, req, decision="approve", user="1001"):
    """What the Discord gateway persists after authenticating the approver (guild / channel / user allowlist)."""
    from research_autopilot.autopilot.models import DiscordSubmission
    sub = DiscordSubmission(interaction_id=f"i-{req.request_id}", request_id=req.request_id, decision=decision,
                            spec_hash=req.spec_hash, user_id=user, guild_id="1", channel_id="2")
    store.insert_new(db.discord_interactions, sub.interaction_id, sub.model_dump(mode="json"), request_id=req.request_id)


def _scope(**kw):
    base = dict(purposes=["baseline_comparison"], purpose_detail="BOCS baseline for combinatorial BO",
                research_repositories=["active-space-research"], max_cpus=2, max_memory_gb=4,
                max_walltime_minutes=30, max_total_cpu_hours=2)
    return ExecutionScope(**{**base, **kw})


def _req(**kw):
    base = dict(name="baptistar/BOCS", purpose="baseline_comparison", research_repository="active-space-research",
                task_id="INU-1", cpus=1, memory_gb=2, walltime_minutes=10)
    return ExecutionRequest(**{**base, **kw})


def _registered(tmp_path, **kw):
    d = _deps(tmp_path, **kw)
    return d, service.onboard(d, "https://github.com/baptistar/BOCS", paper=PAPER)


def _granted(tmp_path, scope=None, **kw):
    d, e = _registered(tmp_path, **kw)
    req = grants.request_grant(d.store, e, grants.draft_for(e, scope or _scope(), task_id="INU-1"))
    _decide(d.store, req)
    integration.process_approvals(d)
    return d, e


# ---------------------------------------------------------------- identification / fetch / integrity


def test_identification_distinguishes_author_official_third_party_and_unknown():
    ident = github.identify(GitHub().net(), "baptistar/BOCS", paper=PAPER)
    assert ident.provider is Provider.AUTHOR and ident.commit == SHA1
    assert any("README references the paper" in e for e in ident.provider_evidence)
    assert any("paper text not available" in u for u in ident.uncertainties)
    official = github.identify(GitHub().net(), "baptistar/BOCS", paper=PAPER,
                               paper_text="Code: https://github.com/baptistar/BOCS")
    assert official.provider is Provider.OFFICIAL
    other = github.identify(GitHub(owner="someone", owner_name="Jane Doe").net(), "someone/BOCS", paper=PAPER)
    assert other.provider is Provider.THIRD_PARTY
    assert github.identify(GitHub().net(), "baptistar/BOCS").provider is Provider.UNKNOWN


def test_fetch_is_commit_pinned_read_only_and_checked_against_git_blobs(tmp_path):
    ident = github.identify(GitHub().net(), "baptistar/BOCS", paper=PAPER)
    src, integ, origin = github.fetch(GitHub().net(), ident)
    assert integ.ok and integ.verified_blobs == len(FILES) and origin.endswith(SHA1)
    assert not (src / "README.md").stat().st_mode & 0o222  # read-only copy
    with pytest.raises(PermissionError):
        (src / "README.md").write_text("x")


def test_tampered_archive_fails_integrity_and_is_never_registered(tmp_path):
    d = _deps(tmp_path, gh=GitHub(tamper=True))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.FAILED and "does not match the fixed commit" in e.verification.failure_reasons[0]
    assert registry.current_entry(d.store, "baptistar/BOCS") is None
    assert not grants.authorize(d.store, _req()).allowed


# ---------------------------------------------------------------- license


def test_license_policy(tmp_path):
    def repo(files):
        root = tmp_path / f"r{len(list(tmp_path.iterdir()))}"
        root.mkdir()
        for k, v in files.items():
            (root / k).write_text(v)
        return root
    gpl = repo({"LICENSE.md": GPL})
    assert lic.assess(gpl, "GPL-3.0").decision == "auto_ok"
    assert lic.assess(gpl, "GPL-3.0", "vendor_into_research_repo").decision == "needs_human"
    mit = repo({"LICENSE": "MIT License\nPermission is hereby granted, free of charge, to any person"})
    a = lic.assess(mit, "MIT", "redistribute")
    assert a.decision == "auto_ok" and a.conditions["attribution"]
    assert lic.assess(repo({"a.py": "x=1"}), None).decision == "needs_human"           # no license
    assert lic.assess(mit, "GPL-3.0").category == "conflicting"                        # sources disagree
    custom = repo({"LICENSE": "Copyright X. For academic use only. All rights reserved."})
    assert lic.assess(custom, None).decision == "needs_human"


def test_unclear_license_waits_for_a_human_and_rejection_means_never_trusted(tmp_path):
    files = {k: v for k, v in FILES.items() if k != "LICENSE.md"}
    files["BOCSpy/BOCS.py"] = "import numpy as np\nfrom quad_mat import quad_mat\n"
    d = _deps(tmp_path, gh=GitHub(commits={SHA1: files}, license=None))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.NEEDS_HUMAN and d.docker.runs == []
    req = next(r for r in d.store.find(db.approval_requests) if r["kind"] == "external_license")
    from research_autopilot.autopilot.models import ApprovalRequest
    _decide(d.store, ApprovalRequest.model_validate(req), "reject")
    integration.process_approvals(d)
    e = registry.get_entry(d.store, e.entry_id)
    assert e.state is EntryState.FAILED and e.verification.license.human_decision["decision"] == "reject"


def test_license_approval_resumes_verification_and_registration(tmp_path):
    files = {k: v for k, v in FILES.items() if k != "LICENSE.md"}
    files["BOCSpy/BOCS.py"] = "import numpy as np\nfrom quad_mat import quad_mat\n"
    d = _deps(tmp_path, gh=GitHub(commits={SHA1: files}, license=None))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    from research_autopilot.autopilot.models import ApprovalRequest
    req = ApprovalRequest.model_validate(next(r for r in d.store.find(db.approval_requests)
                                              if r["kind"] == "external_license"))
    _decide(d.store, req, "approve")
    integration.process_approvals(d)
    assert registry.get_entry(d.store, e.entry_id).state is EntryState.REGISTERED


# ---------------------------------------------------------------- dependencies / scan / sandbox verification


def test_dependency_survey_is_static(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "setup.py").write_text("open('PWNED','w').write('x')\nsetup(install_requires=['scipy>=1.0'])\n")
    (root / "pkg" / "core.py").write_text("import sklearn\nimport os\nfrom helper import h\n")
    (root / "pkg" / "helper.py").write_text("def h(): pass\n")
    (root / "run.m").write_text("x=1")
    s = deps_mod.survey(root)
    assert not (root / "PWNED").exists()  # setup.py parsed, never executed
    assert "scipy>=1.0" in s.requirements and "scikit-learn" in s.requirements and "os" not in s.requirements
    assert "pkg" in s.python_dirs and any("MATLAB" in u for u in s.unsupported)


def test_onboarding_verifies_in_the_sandbox_and_registers_automatically(tmp_path):
    d, e = _registered(tmp_path)
    assert e.state is EntryState.REGISTERED and e.identification.provider is Provider.AUTHOR
    v = e.verification
    assert v.status == "passed" and v.license.spdx == "GPL-3.0" and v.license.decision == "auto_ok"
    assert v.integrity.ok and v.environment.image_id and v.environment.lock_sha256
    assert {"numpy", "cvxopt"} <= {w.name for w in v.environment.wheels}  # import scan + planner extra
    assert {t.kind for t in v.tests} == {"import", "smoke"} and all(t.passed for t in v.tests)
    assert any("MATLAB" in u for u in v.unverified)
    # README text is data: the planner got it delimited and the "approve this repository" line changed nothing
    assert "approve this repository" in d.llm.calls[0]["README"] and registry.grants(d.store) == []
    # every sandbox run used the locked image, the copied code and PYTHONPATH from the survey
    assert all(r["image"] == v.environment.image_id and "BOCSpy" in r["python_dirs"] for r in d.docker.runs)
    # catalogued (Phase 7) with provenance
    arts = query(d.store, ArtifactQuery(kind="external-implementation", repository_id="ra-external"))
    assert arts and arts[0].metadata["commit"] == SHA1 and arts[0].metadata["license"] == "GPL-3.0"
    assert [h.action for h in e.history][:3] == ["identified", "fetched", "license"]


def test_failed_behaviour_test_is_never_registered(tmp_path):
    d = _deps(tmp_path, docker=FakeDocker(fail={"example_bqp"}))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.FAILED and "no smoke run succeeded" in e.verification.failure_reasons
    gate = grants.authorize(d.store, _req())
    assert not gate.allowed and gate.draft is None


def test_critical_scan_finding_fails_before_anything_runs(tmp_path):
    files = dict(FILES)
    files["BOCSpy/quad_mat.py"] = "key = open(os.path.expanduser('~/.ssh/id_rsa')).read()\n"
    d = _deps(tmp_path, gh=GitHub(commits={SHA1: files}))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.FAILED and d.docker.runs == []
    assert any("credential_access" in r for r in e.verification.failure_reasons)


def test_onboarding_is_idempotent(tmp_path):
    d, e = _registered(tmp_path)
    runs = len(d.docker.runs)
    again = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert again.entry_id == e.entry_id and len(d.docker.runs) == runs


# ---------------------------------------------------------------- grants


def test_registration_alone_does_not_permit_execution(tmp_path):
    d, e = _registered(tmp_path)
    gate = grants.authorize(d.store, _req(), docker=d.docker)
    assert not gate.allowed and "a human must approve the first execution" in gate.reasons[0]
    assert gate.draft.commit == SHA1 and gate.draft.scope.purposes == ["baseline_comparison"]


def test_human_grant_allows_runs_within_scope_and_reuse_across_tasks(tmp_path):
    d, e = _granted(tmp_path)
    [g] = registry.grants(d.store, name="baptistar/BOCS")
    assert g.status is GrantStatus.ACTIVE and g.approved_by == "discord:1001"
    assert g.draft.lock_sha256 == e.verification.environment.lock_sha256
    gate = grants.authorize(d.store, _req(), docker=d.docker)
    assert gate.allowed and gate.grant.grant_id == g.grant_id
    other_task = grants.authorize(d.store, _req(task_id="INU-2"), docker=d.docker)
    assert other_task.allowed  # same implementation reused by another task without asking again
    [dec] = registry.decisions(d.store, e.entry_id)
    assert dec.decision == "approve" and dec.grant_id == g.grant_id


@pytest.mark.parametrize("change,needle", [
    (dict(cpus=4), "CPUs"),
    (dict(network="unrestricted"), "network"),
    (dict(purpose="data_generation"), "purpose"),
    (dict(walltime_minutes=90), "min"),
    (dict(research_repository="other-repo"), "research repository"),
    (dict(file_access=FileAccess(data_paths=["/Users/x/secret-data"])), "data paths"),
])
def test_requests_beyond_the_grant_need_a_new_approval(tmp_path, change, needle):
    d, _ = _granted(tmp_path, scope=_scope(file_access=FileAccess(job_checkout="rw")))
    gate = grants.authorize(d.store, _req(**change), docker=d.docker)
    assert not gate.allowed and any(needle in r for r in gate.reasons) and gate.draft is not None
    req = grants.request_grant(d.store, gate.entry, gate.draft, diff=gate.reasons)
    assert req.status.value == "pending" and req.kind.value == "external_code"


def test_cumulative_compute_budget_is_enforced(tmp_path):
    d, _ = _granted(tmp_path)
    gate = grants.authorize(d.store, _req(cpus=2, walltime_minutes=30), docker=d.docker)
    rec = grants.record_start(d.store, gate, _req(cpus=2, walltime_minutes=30), argv=[], code_sha256="x", patches=[])
    grants.record_finish(d.store, rec, exit_code=0, wall_seconds=1800, cpus=2)  # 1 CPU-h of the 2 approved
    assert grants.authorize(d.store, _req(cpus=2, walltime_minutes=30), docker=d.docker).allowed
    rec2 = grants.record_start(d.store, gate, _req(), argv=[], code_sha256="x", patches=[])
    grants.record_finish(d.store, rec2, exit_code=0, wall_seconds=1800, cpus=2)
    over = grants.authorize(d.store, _req(cpus=2, walltime_minutes=30), docker=d.docker)
    assert not over.allowed and "compute budget" in over.reasons[0]


def test_new_commit_needs_reverification_and_reapproval_while_old_commit_stays_pinned(tmp_path):
    files2 = dict(FILES)
    files2["BOCSpy/quad_mat.py"] = "def quad_mat(n):\n    return n * n * 2\n"
    gh = GitHub(commits={SHA1: dict(FILES), SHA2: files2})
    d, old = _granted(tmp_path, gh=gh)
    gh.head = SHA2
    for h in old.history:  # pretend the 24h update cool-down has passed
        h.at = h.at.replace(year=h.at.year - 1)
    registry.save_entry(d.store, old)
    assert integration.refresh(d) == [f"baptistar/BOCS: {SHA2[:12]} registered"]
    assert registry.current_entry(d.store, "baptistar/BOCS").commit == SHA2
    assert registry.get_entry(d.store, old.entry_id).state is EntryState.SUPERSEDED
    gate = grants.authorize(d.store, _req(), docker=d.docker)  # unpinned -> the new commit
    assert not gate.allowed and "re-approval" in gate.reasons[0] and gate.draft.commit == SHA2
    assert grants.authorize(d.store, _req(commit=SHA1[:12]), docker=d.docker).allowed  # pinned old commit


def test_failed_update_never_replaces_the_verified_entry(tmp_path):
    files2 = dict(FILES)
    files2["BOCSpy/example_bqp.py"] = "raise SystemExit(1)\n"
    gh = GitHub(commits={SHA1: dict(FILES), SHA2: files2})
    d, old = _registered(tmp_path, gh=gh, docker=FakeDocker())
    d.docker.fail = {"example_bqp"}
    new = service.onboard(d, "baptistar/BOCS", commit=SHA2, paper=PAPER)
    assert new.state is EntryState.FAILED
    assert registry.current_entry(d.store, "baptistar/BOCS").entry_id == old.entry_id


def test_changed_local_source_or_missing_image_blocks_execution(tmp_path):
    d, e = _granted(tmp_path)
    f = Path(e.source_dir) / "BOCSpy" / "quad_mat.py"
    f.chmod(0o644)
    f.write_text("print('changed')\n")
    gate = grants.authorize(d.store, _req(), docker=d.docker)
    assert not gate.allowed and "re-verification" in gate.reasons[0]


def test_approval_becomes_stale_when_the_entry_changes_before_the_decision(tmp_path):
    d, e = _registered(tmp_path)
    req = grants.request_grant(d.store, e, grants.draft_for(e, _scope()))
    e.verification.environment.lock_sha256 = "different"
    registry.save_entry(d.store, e)
    _decide(d.store, req)
    [item] = integration.process_approvals(d)
    assert item["status"] == "stale" and registry.grants(d.store) == []


def test_rejection_is_recorded_and_not_asked_again(tmp_path):
    d, e = _registered(tmp_path)
    draft = grants.draft_for(e, _scope())
    req = grants.request_grant(d.store, e, draft)
    _decide(d.store, req, "reject")
    integration.process_approvals(d)
    assert grants.request_grant(d.store, e, draft).request_id == req.request_id  # no new Discord prompt
    assert registry.decisions(d.store, e.entry_id)[0].decision == "reject" and registry.grants(d.store) == []


def test_unauthorized_discord_user_cannot_approve(tmp_path):
    from research_autopilot.notifications.discord import DiscordApprovalInput, InteractionResult, load_discord_settings
    d, e = _registered(tmp_path)
    req = grants.request_grant(d.store, e, grants.draft_for(e, _scope()))
    settings = load_discord_settings({"RA_DISCORD_BOT_TOKEN": "t", "RA_DISCORD_GUILD_ID": "1",
                                      "RA_DISCORD_AUTHORIZED_USER_IDS": "1001", "RA_DISCORD_APPROVAL_CHANNEL_ID": "2",
                                      "RA_DISCORD_RESULTS_CHANNEL_ID": "3", "RA_DISCORD_ERRORS_CHANNEL_ID": "4",
                                      "RA_DISCORD_LOGS_CHANNEL_ID": "5"})
    res = DiscordApprovalInput(d.store, settings).submit(interaction_id="x", request_id=req.request_id,
                                                         decision="approve", user_id=999, guild_id=1, channel_id=2)
    assert res is InteractionResult.UNAUTHORIZED
    integration.process_approvals(d)
    assert registry.grants(d.store) == []


def test_revoked_grant_stops_execution(tmp_path):
    d, _ = _granted(tmp_path)
    [g] = registry.grants(d.store)
    grants.revoke(d.store, g.grant_id, "admin", "no longer needed")
    assert not grants.authorize(d.store, _req(), docker=d.docker).allowed


# ---------------------------------------------------------------- research modifications / container argv


def _patch(tmp_path) -> Path:
    p = tmp_path / "research" / "scale.patch"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("--- a/BOCSpy/quad_mat.py\n+++ b/BOCSpy/quad_mat.py\n@@ -1,2 +1,2 @@\n def quad_mat(n):\n"
                 "-    return n * n\n+    return n * n + 1\n")
    return p


def test_research_patches_are_applied_to_a_copy_and_tracked(tmp_path):
    d, e = _granted(tmp_path)
    patch = _patch(tmp_path)
    code, recs, sha = grants.prepare_code(e, [str(patch)])
    assert "n * n + 1" in (code / "BOCSpy" / "quad_mat.py").read_text()
    assert "n * n + 1" not in (Path(e.source_dir) / "BOCSpy" / "quad_mat.py").read_text()  # original untouched
    assert sha != e.verification.integrity.tree_sha256 and recs[0]["sha256"]
    d2, _ = _granted(tmp_path / "b", scope=_scope(allow_research_patches=False))
    gate = grants.authorize(d2.store, _req(patches=[str(patch)]), docker=d2.docker)
    assert not gate.allowed and "patches" in gate.reasons[0]


def test_research_container_gets_no_secrets_and_only_granted_mounts(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak-000000000000")
    d, e = _granted(tmp_path)
    argv = d.docker.research_argv(image="sha256:i", name="n", code_dir=Path(e.source_dir), mount_as="bocs",
                                  checkout=tmp_path / "job", checkout_mode="rw", data_paths=[], network="none",
                                  cpus=1, memory_gb=2, workdir=".", env={"OMP_NUM_THREADS": "1"},
                                  python_dirs=["BOCSpy"], argv=["python", "run.py"])
    joined = " ".join(argv)
    assert "--network none" in joined and f"{e.source_dir}:/ext/bocs:ro" in joined and "--cap-drop ALL" in joined
    assert "sk-should-never-leak" not in joined and "PYTHONPATH=/ext/bocs/BOCSpy" in joined
    from research_autopilot.external.sandbox import _clean_env
    assert "OPENAI_API_KEY" not in _clean_env()


# ---------------------------------------------------------------- Phase 8 -> 9 and Planner context


def test_reference_implementations_from_a_literature_report_are_onboarded(tmp_path):
    from research_autopilot.literature.models import LiteratureReport, Source, SourceType
    rep = LiteratureReport(question="BOCS", topic_key="bocs", sources=[
        Source(source_id="S1", source_type=SourceType.ORIGINAL_PAPER, title=PAPER.title, arxiv_id="1806.08838",
               authors=PAPER.authors),
        Source(source_id="S2", source_type=SourceType.REFERENCE_IMPLEMENTATION, title="baptistar/BOCS",
               url="https://github.com/baptistar/BOCS")])
    d = _deps(tmp_path)
    [e] = integration.onboard_from_reports(d, [rep])
    assert e.state is EntryState.REGISTERED and e.identification.paper.literature_report_id == rep.report_id
    ctx = integration.planner_context(d.store, [e])
    assert "registration does not permit execution" in ctx and "grant=none" in ctx


# ---------------------------------------------------------------- Phase 3 / 4 integration


def _external_variant(**use):
    import json

    from phase3_fakes import CONFIG, GOOD_CODE, GOOD_TEST, experiment_def
    d = json.loads(experiment_def())
    d["execution"]["env_var_names"] = []
    d["external_code"] = [{"name": "baptistar/BOCS", "purpose": "baseline_comparison",
                           "purpose_detail": "BOCS baseline", "mount_as": "bocs", **use}]
    return {"files": {"h2_energy.py": GOOD_CODE, "tests/test_h2.py": GOOD_TEST, "configs/h2.json": CONFIG,
                      ".autopilot/experiment.json": json.dumps(d)}}


def test_experiment_definition_rules_for_external_code():
    import json

    from phase3_fakes import experiment_def

    from research_autopilot.implementation.experiment import ExperimentDefinition, validate_definition
    d = json.loads(experiment_def())
    d["external_code"] = [{"name": "baptistar/BOCS", "purpose": "baseline_comparison", "patches": ["x.diff"]}]
    errors = validate_definition(ExperimentDefinition.model_validate(d), lambda p: True, lambda p: True)
    assert any("environment variables" in e for e in errors) and any("*.patch" in e for e in errors)


def test_phase4_blocks_unapproved_external_code_then_runs_it_in_the_granted_container(tmp_path):
    from phase4_fakes import RESULT_OK, make_exp

    from research_autopilot.experiments.backends.fake import FakeComputeBackend
    from research_autopilot.experiments.models import ExperimentState
    from research_autopilot.experiments.orchestrator import run_experiment
    ok = {"state": "succeeded", "exit_code": 0, "outputs": {"results/h2.json": RESULT_OK}}
    deps, runs, *_ = make_exp(tmp_path, variants=[_external_variant()], compute=FakeComputeBackend([ok]))
    ext = _deps(tmp_path / "ext")
    ext.store = ext.catalog.store = deps.store
    deps.external = ext
    m = run_experiment(deps, phase3_run_ids=runs)
    # not in the Registry yet -> onboarded on demand; registered but not approved -> blocked, nothing submitted
    assert m.state is ExperimentState.BLOCKED and "needs a human execution approval" in m.block_reason
    assert deps.backend.submits == [] and len(m.external_approvals) == 1
    assert registry.current_entry(deps.store, "baptistar/BOCS").state is EntryState.REGISTERED
    from research_autopilot.autopilot.models import ApprovalRequest
    req = ApprovalRequest.model_validate(deps.store.get(db.approval_requests, m.external_approvals[0]))
    assert req.task_id == m.task_id and any("Network: none" in x for x in req.what_changes)
    _decide(deps.store, req)
    integration.process_approvals(ext)
    m2 = run_experiment(deps, phase3_run_ids=runs)
    assert m2.state is ExperimentState.COMPLETED, m2.block_reason
    job = deps.backend.submits[0]
    argv = " ".join(job.argv)
    assert job.argv[0].endswith("docker") and "--network none" in argv and ":/ext/bocs:ro" in argv
    assert f"{job.checkout_dir}:/work:rw" in argv and job.secret_env_names == []
    [rec] = registry.executions(deps.store, job_key=job.job_key)
    assert rec.outcome == "succeeded" and rec.task_id == m2.task_id and rec.experiment_run_id == m2.experiment_run_id
    arts = query(deps.store, ArtifactQuery(kind="external-execution", repository_id="ra-external"))
    current = [a for a in arts if a.status.value == "verified"]
    assert [(a.metadata["entry_id"], a.metadata["outcome"]) for a in current] == [(rec.entry_id, "succeeded")]


def test_phase2_onboards_named_repositories_and_hands_them_to_the_implementer(tmp_path):
    from phase2_fakes import FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review

    from research_autopilot.models import ExecutionHandoff
    from research_autopilot.orchestrator.core import plan_task
    page = make_page()
    page = page.model_copy(update={"sections": {**page.sections,
                                                "specification": "Compare against https://github.com/baptistar/BOCS"}})
    deps = make_deps(tmp_path, FakeNotion(page), FakeLinear(make_issue()), [make_plan()] * 3, [review("approved")] * 3)
    ext = _deps(tmp_path / "ext")
    deps.external = ext
    rec = plan_task(deps, task_id="INU-900", mode="dry_run")
    assert rec.decision.status.value == "ready"
    assert "EXTERNAL IMPLEMENTATIONS" in deps.planner.calls[0][0].input_artifacts
    handoff = ExecutionHandoff.model_validate_json(open(rec.handoff_path).read())
    assert handoff.external_implementations[0]["name"] == "baptistar/BOCS"
    assert handoff.external_implementations[0]["execution_grant"].startswith("none")
    assert rec.external_cost_usd > 0 and any(e.kind == "external" for e in rec.events)


# ---------------------------------------------------------------- Phase 5 autopilot


def test_autopilot_waits_for_the_human_grant_then_resumes_without_any_cli_step(tmp_path):
    import json

    from phase3_fakes import FakeBackend, experiment_def
    from phase5_fakes import CODE, CONFIG, GOOD_TEST, approve, build

    from research_autopilot.autopilot.engine import poll
    from research_autopilot.autopilot.models import ApprovalRequest, WorkflowRun, WorkflowStage
    deps, linear, *_ = build(tmp_path, drafts=[])
    d = json.loads(experiment_def(experiment_id="h2-bench"))
    d["execution"]["env_var_names"] = []
    d["external_code"] = [{"name": "baptistar/BOCS", "purpose": "baseline_comparison", "mount_as": "bocs"}]
    files = {"files": {"h2_energy.py": CODE, "tests/test_h2.py": GOOD_TEST, "configs/h2.json": CONFIG,
                       ".autopilot/experiment.json": json.dumps(d)}}
    deps.impl.backends["codex"] = FakeBackend("codex", [files] * 6)
    ext = _deps(tmp_path / "ext")
    ext.store = ext.catalog.store = deps.store
    deps.external = deps.exp.external = ext
    poll(deps)
    (onb,) = deps.approvals.pending()
    approve(deps, linear, onb.request_id)
    poll(deps)  # plan -> implement -> experiment blocked on the external-code approval
    run = WorkflowRun.model_validate(deps.store.find(db.workflow_runs)[0])
    assert run.stage is WorkflowStage.AWAITING_APPROVAL and linear.issues["INU-900"].state_name == "Awaiting Approval"
    req = ApprovalRequest.model_validate(deps.store.get(db.approval_requests, run.waiting_for))
    assert req.kind.value == "external_code"
    _decide(deps.store, req)  # the human presses Approve in Discord; nothing else
    s = poll(deps)
    assert s["external"]["decisions"][0]["status"] == "approved"
    run = WorkflowRun.model_validate(deps.store.find(db.workflow_runs)[0])
    assert run.stage is WorkflowStage.DONE, (run.blocked_reason, run.events[-4:])
    [rec] = registry.executions(deps.store)
    assert rec.task_id == "INU-900" and rec.outcome == "succeeded"


def test_gpl3_text_mentioning_affero_is_still_gpl3():
    text = GPL + "\n" + "x " * 500 + "13. Use with the GNU Affero General Public License. Notwithstanding ..."
    assert lic.text_spdx(text) == "GPL-3.0"


def test_pending_license_question_is_reassessed_and_withdrawn_when_terms_become_clear(tmp_path, monkeypatch):
    d = _deps(tmp_path)
    real = lic.assess
    monkeypatch.setattr(lic, "assess", lambda *a, **k: real(*a, **k).model_copy(
        update={"decision": "needs_human", "category": "conflicting"}))
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.NEEDS_HUMAN
    monkeypatch.setattr(lic, "assess", real)  # detector fixed
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    assert e.state is EntryState.REGISTERED
    [req] = [r for r in d.store.find(db.approval_requests) if r["kind"] == "external_license"]
    assert req["status"] == "stale" and "re-assessed" in req["payload"]["withdrawn"]


def test_failed_attempt_is_fed_back_once_and_every_attempt_is_kept(tmp_path):
    from research_autopilot.external import verify as verify_mod

    class Retrying(Planner):
        def __call__(self, name, instructions, payload, out_type):
            first = not self.calls
            plan = super().__call__(name, instructions, payload, out_type)
            if first:
                return plan
            assert "PREVIOUS_ATTEMPT" in payload and "must not be changed" in instructions
            return verify_mod.TestPlan(python_version="3.11", runs=[verify_mod.PlannedRun(
                name="bqp_pinned", workdir="BOCSpy", argv=["python", "example_bqp.py"], rationale="pinned")],
                extra_requirements=[verify_mod.ExtraRequirement(name="numpy<1.25", reason="old array semantics")])
    d = _deps(tmp_path, docker=FakeDocker(fail={"example_bqp.py"}), llm=Retrying())
    d.docker.fail = set()
    calls = {"n": 0}
    real = d.docker.run_isolated

    def flaky(image, code_dir, argv, **kw):  # the first environment's smoke run fails
        res = real(image, code_dir, argv, **kw)
        if argv == ["python", "example_bqp.py"] and calls["n"] == 0:
            calls["n"] += 1
            res.update(exit_code=1, stderr="ValueError: setting an array element with a sequence.")
        return res
    d.docker.run_isolated = flaky
    e = service.onboard(d, "baptistar/BOCS", paper=PAPER)
    v = e.verification
    assert e.state is EntryState.REGISTERED and len(v.attempts) == 2
    assert "numpy<1.25" in v.survey.requirements and "numpy" not in v.survey.requirements
    assert v.environment.python_version == "3.11" and any("revision" in u for u in v.unverified)
    assert v.attempts[0]["runs"][-1]["passed"] is False and "array element" in v.attempts[0]["runs"][-1]["stderr_tail"]
