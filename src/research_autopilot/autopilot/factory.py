"""Build real Phase 5 dependencies from the environment (credentials never printed)."""

from research_autopilot.autopilot.approvals import ApprovalController
from research_autopilot.autopilot.completion import OpenAICompletionReviewer
from research_autopilot.autopilot.engine import AutopilotDeps, AutopilotSettings, notify_approval, task_urls
from research_autopilot.autopilot.dependency_inference import OpenAIDependencyInferer
from research_autopilot.autopilot.flows import PrefectConfirmationChannel
from research_autopilot.autopilot.linear_ops import LinearOps
from research_autopilot.autopilot.outbox import Outbox
from research_autopilot.autopilot.settings import load_phase5_settings
from research_autopilot.config import load_dotenv, load_settings
from research_autopilot.experiments.factory import build_exp_deps
from research_autopilot.notifications import get_notifier
from research_autopilot.orchestrator.factory import build_deps
from research_autopilot.storage.db import Store
from research_autopilot.tasks.linear import LinearWriter
from research_autopilot.tasks.notion_writer import NotionWriter


def store_from_env() -> Store:
    load_dotenv()
    return Store(load_settings().resolved_database_url())


def build_autopilot_deps(dry_run: bool = False) -> AutopilotDeps:
    load_dotenv()
    p2 = load_settings()
    s5 = load_phase5_settings()
    if s5.write_enabled and not dry_run:
        p2 = p2.model_copy(update={"linear_write_enabled": True})  # Phase 2 apply follows the Phase 5 write switch
    plan = build_deps(p2)
    exp = build_exp_deps()
    impl = exp.impl_deps
    import os
    assignee = os.environ.get("RA_LINEAR_ASSIGNEE", "none").strip().lower() or "none"
    if assignee not in ("none", "me"):
        raise ValueError(f"RA_LINEAR_ASSIGNEE must be 'none' or 'me', not {assignee!r}")
    linear = LinearWriter(p2.linear_api_key.get_secret_value(), p2.linear_project_id, assign_to_viewer=assignee == "me")
    writer = NotionWriter(p2.notion_api_token.get_secret_value(), p2.notion_tasks_data_source_id, s5.notes_data_source_id)
    ops = LinearOps(linear, plan.store, test_only=s5.test_only, write_enabled=s5.write_enabled and not dry_run)
    channel = PrefectConfirmationChannel(s5.confirm_deployment, s5.prefect_ui_url)
    notifier = get_notifier(store=plan.store)
    impl.notifier = notifier
    plan.notifier = notifier  # planner / plan-reviewer hand-overs to #logs
    def notify(req, msg):
        # Real Notion page / Linear issue URLs of the task (not a hard-coded workspace URL).
        notion_url, linear_url = task_urls(plan.manager, req.task_id) if req.status.value == "pending" else (None, None)
        notify_approval(plan.store, notifier, req, msg, linear_url, channel.url(req.confirmation_ref), notion_url)

    approvals = ApprovalController(plan.store, ops, channel if s5.approval_mode == "linear+form" else None,
                                   notify=notify, mode=s5.approval_mode, approver_ids=set(s5.approver_linear_user_ids))
    dependency_inferer = OpenAIDependencyInferer(s5.dag_inference_model or p2.reviewer_model, p2.model_pricing,
                                                 lambda: s5.budget_api_usd, p2.agents_tracing)
    from research_autopilot.research.workflow import ResearchDeps, llm_factory_from_env
    from research_autopilot.autopilot.publish import GitHubPublisher
    push_token = os.environ.get("RA_GITHUB_PUSH_TOKEN", "").strip()
    publisher = GitHubPublisher(push_token) if push_token else None
    return AutopilotDeps(external=exp.external, research=ResearchDeps(llm_factory=llm_factory_from_env(p2)), settings=AutopilotSettings(default_budget=s5.budget, prefect_ui_url=s5.prefect_ui_url,
                                                    max_concurrent_tasks=s5.max_concurrent_tasks, dry_run=dry_run),
                         plan=plan, impl=impl, exp=exp, linear=linear, notion_writer=writer, ops=ops, approvals=approvals,
                         outbox=Outbox(plan.store, writer, linear, ops),
                         completion_reviewer=OpenAICompletionReviewer(p2.reviewer_model, p2.model_pricing),
                         notifier=notifier, dependency_inferer=dependency_inferer, publisher=publisher)
