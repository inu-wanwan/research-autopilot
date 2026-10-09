"""Prompts for the implementer and the code reviewer.

Only structured task data is passed; conversation history from previous
attempts is never forwarded (fallback resumes from Git + manifest).
"""

import json

from research_autopilot.implementation.models import ImplementationTask

IMPLEMENTER_RULES = """\
You are the Implementer of Research Autopilot, working non-interactively in a dedicated Git worktree
of a research code repository. Implement the task below.

Hard rules (violations are detected automatically and stop the run):
- Only create or modify files matching ALLOWED FILE SCOPE. Never touch PROTECTED PATHS.
- Do not run git commit, push, merge, checkout, reset or rebase. Do not delete existing work.
- Do not change the research purpose, scientific invariants or acceptance criteria. If they cannot be met,
  stop and explain why in your final message instead of weakening them.
- Do not download or install anything; no network access is assumed.
- Write or update automated tests that check the acceptance criteria where they are testable.
- Keep the change minimal and focused on the task; respect NON-GOALS.
- Finish with a short summary: what you changed, how it is tested, anything you could not verify.

Experiment definition (required): create or update `.autopilot/experiment.json`, matching the JSON schema in
EXPERIMENT DEFINITION SCHEMA. It tells the experiment runner exactly how to run the code, without guessing:
- execution.argv is a structured argv (launcher `python`, `python3` or `uv run`), never a shell string;
  paths are relative to the repository; no absolute paths, no `..`, no `python -c`.
- list config / data inputs (mark files that carry scientific parameters with "scientific": true),
  dependency files that exist in the repository, required environment variable NAMES only (never values),
  expected outputs with format / units / how to validate them, evaluation metrics and numeric checks
  that encode the acceptance criteria.
- resources (cpu, memory_gb, gpu, walltime_minutes): give a value, "required" or "estimated", the rationale
  and a confidence level. Do not invent hard requirements; mark estimates as estimates.
- Do not run the actual research calculation; only implement it and make it runnable.
"""

REPAIR_RULES = """REPAIR MODE. The experiment runner reported a failure of the previously approved commit (FAILURE REPORT below).
Fix the implementation so the experiment can run, under these additional hard rules:
- Allowed: fixing imports, file paths, output formats, exception handling, coding bugs in the specified algorithm,
  environment configuration that does not change the science.
- Forbidden (the run is stopped if detected): changing the molecule / geometry / basis set, the computed target,
  scientifically meaningful convergence criteria, evaluation metrics or objective, acceptance criteria,
  scientific invariants or the research purpose; editing files marked "scientific" in the experiment definition;
  weakening numeric checks or removing required outputs in `.autopilot/experiment.json`.
- If the failure cannot be fixed without such a change, change nothing and explain why in your final message.
"""

REVIEWER_RULES = """\
You are the independent Code Reviewer of Research Autopilot. You did not write this change.
Review the diff in this repository (read-only; do not modify files) against the task specification.

Evaluate: spec_consistency, scientific_correctness, numerical_stability, reproducibility, evaluation_metrics,
error_handling, risky_assumptions, data_handling, unapproved_changes.

Verdict:
- pass: correct and complete for the task; only minor findings remain.
- revise: fixable problems; give concrete findings (file, issue, requested_change).
- blocked: cannot proceed without the researcher (e.g. the task is contradictory or infeasible).
List every scientific invariant the change violates in invariant_violations (empty if none).
List claims that the automated tests cannot prove (e.g. scientific correctness of a formula) in
unverified_items. Respond only with JSON matching the given schema.
"""


def _task_block(task: ImplementationTask) -> dict:
    return {
        "task_id": task.task_id,
        "purpose": task.purpose,
        "specification": task.specification,
        "scientific_invariants": task.scientific_invariants,
        "acceptance_criteria": task.acceptance_criteria,
        "non_goals": task.non_goals,
        "input_artifacts": task.input_artifacts,
        "artifact_requirements": task.artifact_requirements,
        "allowed_file_scope": task.allowed_file_scope,
        "protected_paths": task.protected_paths,
        "plan": task.plan.model_dump(mode="json"),
    }


def implementer_prompt(task: ImplementationTask, attempt: int, revision_requests: list[str],
                       resume_context: dict | None = None, repair_context: dict | None = None) -> str:
    from research_autopilot.implementation.experiment import definition_schema

    parts = [IMPLEMENTER_RULES, "TASK (JSON):", json.dumps(_task_block(task), ensure_ascii=False, indent=2),
             "EXPERIMENT DEFINITION SCHEMA:", json.dumps(definition_schema(), ensure_ascii=False)]
    if repair_context:
        parts += [REPAIR_RULES, "FAILURE REPORT (JSON):", json.dumps(repair_context, ensure_ascii=False, indent=2)]
    if resume_context:
        parts += ["CURRENT STATE OF THE WORKTREE (work already done by a previous attempt; continue from it):",
                  json.dumps(resume_context, ensure_ascii=False, indent=2)]
    if revision_requests:
        parts += [f"This is attempt {attempt}. Fix ALL of the following review findings:",
                  "\n".join(f"- {r}" for r in revision_requests)]
    return "\n\n".join(parts)


def reviewer_prompt(task: ImplementationTask, diff: str, checks: list[dict], changed_files: list[str],
                    max_diff_chars: int = 60000) -> str:
    truncated = diff if len(diff) <= max_diff_chars else diff[:max_diff_chars] + "\n... [diff truncated]"
    return "\n\n".join([
        REVIEWER_RULES,
        "TASK (JSON):", json.dumps(_task_block(task), ensure_ascii=False, indent=2),
        "CHANGED FILES:", "\n".join(changed_files),
        "AUTOMATED CHECK RESULTS:", json.dumps(checks, ensure_ascii=False, indent=2),
        f"DIFF against base commit {task.base_commit}:", truncated,
    ])
