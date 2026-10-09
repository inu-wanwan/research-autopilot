"""Read-only review sessions on the CLI backends (independent from the implementer's session)."""

import json
from pathlib import Path

from research_autopilot.implementation.backends.base import BackendRequest
from research_autopilot.implementation.backends.claude import ClaudeBackend
from research_autopilot.implementation.backends.codex import CodexBackend
from research_autopilot.implementation.models import CodeReviewResult, ImplementationResult, ImplementationTask, ModelChoice
from research_autopilot.implementation.reviewer import parse_review

REVIEW_TIMEOUT = 900


class CodexReviewRunner:
    def __init__(self, backend: CodexBackend | None = None) -> None:
        self.backend = backend or CodexBackend()

    def __call__(self, task: ImplementationTask, choice: ModelChoice, prompt: str, schema: dict,
                 log_path: Path) -> tuple[ImplementationResult, CodeReviewResult | None]:
        schema_file = log_path.with_suffix(".schema.json")
        schema_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file.write_text(json.dumps(schema))
        req = BackendRequest(task=task, choice=choice, prompt=prompt, timeout_seconds=REVIEW_TIMEOUT, log_path=log_path)
        result = self.backend.run(req, lambda pid: None, sandbox="read-only",
                                  extra_args=["--output-schema", str(schema_file)])
        return result, parse_review(result.summary) if result.status == "completed" else None


class ClaudeReviewRunner:
    def __init__(self, backend: ClaudeBackend | None = None, max_budget_usd: float = 1.0) -> None:
        self.backend = backend or ClaudeBackend()
        self.max_budget_usd = max_budget_usd

    def __call__(self, task: ImplementationTask, choice: ModelChoice, prompt: str, schema: dict,
                 log_path: Path) -> tuple[ImplementationResult, CodeReviewResult | None]:
        req = BackendRequest(task=task, choice=choice, prompt=prompt, timeout_seconds=REVIEW_TIMEOUT,
                             log_path=log_path, max_budget_usd=self.max_budget_usd)
        result = self.backend.run(req, lambda pid: None, json_schema=json.dumps(schema), read_only=True)
        return result, parse_review(result.summary) if result.status == "completed" else None
