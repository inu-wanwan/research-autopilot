"""Phase 5 fixtures: Fake Linear (with history whose actor is the API-key user, like the real one),
Fake Notion writer feeding the Fake Notion reader, Fake confirmation channel, and real Phase 2/3/4
orchestrators wired with their fakes."""

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

from phase2_fakes import PROJECT, FakeLinear, FakeNotion, make_deps, make_issue, make_page, make_plan, review
from phase3_fakes import GOOD_TEST, FakeBackend, FakeReviewer, code_review, experiment_def, make_repo, repo_config
from phase4_fakes import FakeAnalyst, FakeSciReviewer, host
from research_autopilot.autopilot.approvals import ApprovalController, record_submission
from research_autopilot.autopilot.completion import CompletionReview
from research_autopilot.autopilot.engine import AutopilotDeps, AutopilotSettings
from research_autopilot.autopilot.linear_ops import LinearOps
from research_autopilot.autopilot.models import CriterionResult, FormSubmission
from research_autopilot.autopilot.outbox import Outbox
from research_autopilot.experiments.analyst import LlmRun
from research_autopilot.experiments.backends.fake import FakeComputeBackend
from research_autopilot.experiments.config import Phase4Settings
from research_autopilot.experiments.models import ParameterChange, ProposalDraft
from research_autopilot.experiments.orchestrator import ExpDeps
from research_autopilot.implementation.config import Phase3Settings
from research_autopilot.implementation.orchestrator import ImplDeps
from research_autopilot.models import NotionTask, utcnow
from research_autopilot.planning.agents import Usage
from research_autopilot.tasks.notion import notion_ids_in_text
from research_autopilot.tasks.sections import split_sections

USER = {"id": "u1", "name": "Rikushi"}  # API key user == human user (real Linear behaviour)
INV = ["Output schema is fixed"]
ACC = ["results file contains e_rhf, e_fci and e_corr", "e_fci is not above e_rhf"]
SECTIONS = {
    "purpose": "Compute a small benchmark value and store it for later comparison.",
    "specification": "Implement h2_energy.py that writes results/h2.json with e_rhf, e_fci and e_corr.",
    "acceptance_criteria": "\n".join(f"- {a}" for a in ACC),
    "scientific_invariants": "\n".join(f"- {i}" for i in INV),
    "repository": "h2-test",
    "file_scope": "- h2_energy.py\n- tests/**\n- configs/**",
}
RESULT = json.dumps({"e_rhf": -1.1168, "e_fci": -1.1373, "e_corr": -0.0205})
CODE = "def energy():\n    return -1.137\n"
CONFIG = json.dumps({"basis": "sto-3g"}) + "\n"
OK = {"state": "succeeded", "exit_code": 0, "outputs": {"results/h2.json": RESULT}}


def impl_files(exp_id: str = "h2-bench") -> dict:
    return {"files": {"h2_energy.py": CODE, "tests/test_h2.py": GOOD_TEST, "configs/h2.json": CONFIG,
                      ".autopilot/experiment.json": experiment_def(experiment_id=exp_id)}}


class FakeLinearFull(FakeLinear):
    STATES = {**FakeLinear.STATES, "Awaiting Approval": "started", "Approved": "started",
              "Revision Requested": "started", "Duplicate": "canceled"}

    def __init__(self, *issues, missing_states=()):
        super().__init__(*issues)
        self.histories: dict[str, list[dict]] = {i.identifier: [] for i in issues}
        self.comments: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.fail_create = 0
        self.missing_states = set(missing_states)
        self.titles: dict[str, str] = {i.identifier: i.title for i in issues}

    def _hist(self, identifier, frm, to, actor=USER, bot=None, at=None):
        self.histories.setdefault(identifier, []).append({
            "id": f"h{len(self.histories.get(identifier, []))}", "createdAt": (at or utcnow()).isoformat(),
            "actor": actor, "botActor": bot, "fromState": {"name": frm}, "toState": {"name": to}})

    def team_states(self, team_id):
        return {n: (f"state-{n}", t) for n, t in self.STATES.items() if n not in self.missing_states}

    def label_ids(self, names):
        return [f"label-{n}" for n in names]

    def set_state(self, issue_id, state_id):
        issue = next(i for i in self.issues.values() if i.id == issue_id)
        frm = issue.state_name
        name = super().set_state(issue_id, state_id)
        # Linear returns the current state on inverse-relation nodes. Keep the
        # fake faithful so a completed DAG predecessor releases its consumers.
        completed = self.STATES[name]
        for other in self.issues.values():
            other.blocked_by = [b.model_copy(update={"state_name": name, "state_type": completed})
                                if b.identifier == issue.identifier else b for b in other.blocked_by]
        self._hist(issue.identifier, frm, name)  # Autopilot's own write: actor is the API-key user
        return name

    def human_move(self, identifier, to, actor=USER, bot=None, at=None):
        i = self.issues[identifier]
        frm = i.state_name
        i.state_name, i.state_type = to, self.STATES[to]
        self._hist(identifier, frm, to, actor=actor, bot=bot, at=at)

    def create_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return f"c{len(self.comments)}"

    def create_issue(self, team_id, title, description, state_id, label_ids):
        if self.fail_create > 0:
            self.fail_create -= 1
            raise RuntimeError("linear API timeout (simulated)")
        n = 901 + len(self.created)
        ident = f"INU-{n}"
        state = state_id.removeprefix("state-")
        issue = make_issue(ident, state=(state, self.STATES[state]), labels=tuple(x.removeprefix("label-") for x in label_ids),
                           notion_ids=tuple(notion_ids_in_text(description)))
        issue = issue.model_copy(update={"title": title, "description": description})
        self.issues[ident] = issue
        self.histories[ident] = []
        self.created.append(ident)
        return issue.id, ident, f"https://linear.app/x/issue/{ident}"

    def find_by_marker(self, marker):
        return [{"id": i.id, "identifier": i.identifier, "url": i.url, "title": i.title}
                for i in self.issues.values() if marker in i.title]

    def history(self, identifier):
        return list(self.histories.get(identifier, []))


class FakeNotionWriter:
    def __init__(self, notion: FakeNotion):
        self.notion = notion
        self.tasks_ds, self.notes_ds = "tasks-ds", "notes-ds"
        self.notes: dict[str, dict] = {}
        self.fail_notes = 0
        self.created_tasks: list[str] = []

    def find_by_marker(self, ds, prop, marker):
        if ds == self.notes_ds:
            return [k for k, v in self.notes.items() if marker in v["title"]]
        return [p.page_id for p in self.notion.pages.values() if marker in p.title]

    def create_task_page(self, title, blocks, *, pending, category, parent_page_id):
        pid = f"{len(self.notion.pages) + 1:08d}-0000-0000-0000-000000000000"
        lines = []
        for b in blocks:
            t = b["type"]
            text = "".join(x["text"]["content"] for x in b[t]["rich_text"])
            lines.append((2 if t == "heading_2" else 0, ("- " if t == "bulleted_list_item" else "") + text))
        sections, other, pre = split_sections(lines)
        self.notion.pages[pid] = NotionTask(page_id=pid, url=f"https://www.notion.so/{pid.replace('-', '')}", title=title,
                                            status="To do", pending=pending, linear_issue_id=None, category=category,
                                            last_edited_time="2026-10-10T00:00:00.000Z", sections=sections,
                                            other_sections=other, preamble=pre, fetched_at=utcnow())
        self.created_tasks.append(pid)
        return pid, self.notion.pages[pid].url

    def set_task_links(self, page_id, linear_identifier=None, pending=None):
        p = self.notion.pages[page_id]
        upd = {}
        if linear_identifier is not None:
            upd["linear_issue_id"] = linear_identifier
        if pending is not None:
            upd["pending"] = pending
        self.notion.pages[page_id] = p.model_copy(update=upd)

    def project_execution(self, page_id, *, status, run_url=None, error=None, acknowledge=False):
        p = self.notion.pages[page_id]
        self.notion.pages[page_id] = p.model_copy(update={"execution_status": status, "last_run_url": run_url or p.last_run_url,
                                                           "execution_error": error, "run_requested": False if acknowledge else p.run_requested})

    def create_note(self, title, blocks, category="結果"):
        if self.fail_notes > 0:
            self.fail_notes -= 1
            raise RuntimeError("notion 502 (simulated)")
        pid = f"note-{len(self.notes) + 1}"
        self.notes[pid] = {"title": title, "blocks": blocks}
        return pid, f"https://www.notion.so/{pid}"

    def page_url(self, page_id):
        return f"https://www.notion.so/{page_id.replace('-', '')}"


class FakeChannel:
    def __init__(self):
        self.opened = []

    def open(self, req):
        self.opened.append(req.request_id)
        return f"flowrun-{req.request_id[:8]}"

    def url(self, ref):
        return f"http://prefect/runs/{ref}"


def draft(title="Replicate the benchmark twice", replicates="2", invariants=None, acceptance=None, changes=None):
    return ProposalDraft(
        title=title, research_question="Is the value reproducible?", hypothesis="Replicates agree within 1e-8.",
        purpose=SECTIONS["purpose"], implementation="Run the same experiment again with replicates.",
        acceptance_criteria=list(ACC if acceptance is None else acceptance),
        scientific_invariants=list(INV if invariants is None else invariants),
        expected_information_value="reproducibility", dependencies=[], estimated_resources="1 CPU, 1 min",
        estimated_cost="negligible", risks_and_assumptions=[],
        parameter_changes=changes if changes is not None else [
            ParameterChange(name="replicates", from_value="1", to_value=replicates, kind="within_spec")])


class FakeCompletionReviewer:
    model = "gpt-5.6-sol"

    def __init__(self, status="met", outcome="supported", evidence=("check:fci_below_rhf",)):
        self.status, self.outcome, self.evidence = status, outcome, list(evidence)
        self.calls = 0

    def review(self, payload):
        self.calls += 1
        out = CompletionReview(
            criteria=[CriterionResult(criterion=c, status=self.status, evidence=self.evidence, note="")
                      for c in payload["task"]["acceptance_criteria"]],
            hypothesis_outcome=self.outcome, summary="ok", limitations=[], unresolved=[])
        return LlmRun(output=out, usage=Usage(input_tokens=100, output_tokens=50), model=self.model, cost_usd=0.001)


def build(tmp: Path, *, drafts=None, compute=None, n=12, root_title="[TEST] Benchmark task", settings=None,
          completion=None, missing_states=(), test_only=True, sci_reviews=None, approval_mode="linear+form"):
    repo = make_repo(tmp)
    root_issue = make_issue("INU-900").model_copy(update={"title": root_title})
    linear = FakeLinearFull(root_issue, missing_states=missing_states)
    notion = FakeNotion(make_page(sections=dict(SECTIONS)))
    notion.pages[next(iter(notion.pages))] = next(iter(notion.pages.values())).model_copy(update={"title": root_title, "run_requested": True, "execution_status": "Requested"})
    notion.requested_task_ids = lambda: [p.page_id for p in notion.pages.values() if p.run_requested]
    from research_autopilot.config import ModelPrice
    prices = {m: ModelPrice(input_per_1m=1.0, output_per_1m=4.0) for m in ("gpt-5.6-terra", "gpt-5.6-sol")}
    plan = make_deps(tmp, notion, linear, [make_plan()] * n, [review("approved")] * n, linear_write_enabled=True,
                     model_pricing=prices)
    impl = ImplDeps(settings=Phase3Settings(worktrees_dir=tmp / "worktrees"), phase2=plan.settings,
                    registry={"h2-test": repo_config(repo)}, store=plan.store, manager=plan.manager,
                    backends={"codex": FakeBackend("codex", [impl_files()] * n), "claude": FakeBackend("claude", [])},
                    reviewers={"codex": FakeReviewer("codex", [code_review("pass")] * n), "claude": FakeReviewer("claude", [])})
    exp = ExpDeps(settings=Phase4Settings(artifact_root=tmp / "experiments", env_cache_dir=tmp / "envs", retry_backoff_seconds=0),
                  phase2=plan.settings, store=plan.store, manager=plan.manager, backend=compute or FakeComputeBackend([OK] * n),
                  registry=impl.registry, analyst=FakeAnalyst(drafts if drafts is not None else [draft()]),
                  reviewer=FakeSciReviewer(sci_reviews), impl_deps=impl, detect_host=lambda: host(), sleep=lambda s: None,
                  prepare_env=False)
    ops = LinearOps(linear, plan.store, test_only=test_only, write_enabled=True)
    writer = FakeNotionWriter(notion)
    channel = FakeChannel()
    deps = AutopilotDeps(settings=settings or AutopilotSettings(), plan=plan, impl=impl, exp=exp, linear=linear,
                         notion_writer=writer, ops=ops,
                         approvals=ApprovalController(plan.store, ops, channel, mode=approval_mode, approver_ids={USER["id"]}),
                         outbox=Outbox(plan.store, writer, linear, ops),
                         completion_reviewer=completion if completion is not None else FakeCompletionReviewer())
    return deps, linear, notion, writer, channel


def approve(deps, linear, req_id, decision="approve", spec_hash=None, actor=USER, linear_state=None):
    """Simulate the human: move the Linear status and submit the Prefect form."""
    from research_autopilot.autopilot.models import ApprovalRequest
    from research_autopilot.storage import db
    req = ApprovalRequest.model_validate(deps.store.get(db.approval_requests, req_id))
    state = linear_state or {"approve": "Approved", "reject": "Canceled", "revise": "Revision Requested"}[decision]
    linear.human_move(req.task_id, state, actor=actor, at=utcnow() + timedelta(seconds=1))
    record_submission(deps.store, FormSubmission(request_id=req_id, decision=decision, spec_hash=spec_hash or req.spec_hash,
                                                 approver="rikushi"))
    return req
