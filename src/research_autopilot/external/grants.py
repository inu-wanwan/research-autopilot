"""Human execution grants and the runtime gate.

* A grant is created only from a verified Discord decision of an authorized approver on an approval request
  whose hash still matches the current Registry entry (commit, source tree, locked wheels, image).
* ``authorize`` is evaluated before every research execution: the entry must be verified and unchanged on disk,
  and some active grant must cover purpose, research repository, file access, network, resources and the
  cumulative compute budget.  Anything outside a grant yields a draft for a *new* approval instead of running.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from research_autopilot.autopilot.approvals import ApprovalController
from research_autopilot.autopilot.models import ApprovalKind, ApprovalRequest, ApprovalStatus
from research_autopilot.external import registry
from research_autopilot.external.github import ext_root, tree_sha256
from research_autopilot.external.models import (EntryState, ExecutionGrant, ExecutionRecord, ExecutionRequest,
                                                ExecutionScope, FileAccess, GateResult, GrantDecisionRecord,
                                                GrantDraft, GrantStatus, RegistryEntry, stable_hash)
from research_autopilot.models import utcnow
from research_autopilot.storage import db

KINDS = (ApprovalKind.EXTERNAL_CODE, ApprovalKind.EXTERNAL_LICENSE)


class _NoIssue:
    """Approval checks for external code are not tied to a Linear issue state."""

    state_name = "n/a"


# ---------------------------------------------------------------- drafts / requests


def draft_for(entry: RegistryEntry, scope: ExecutionScope, *, task_id: str | None = None,
              reason: str = "") -> GrantDraft:
    v = entry.verification
    env = v.environment
    return GrantDraft(entry_id=entry.entry_id, name=entry.name, commit=entry.commit,
                      tree_sha256=v.integrity.tree_sha256, lock_sha256=env.lock_sha256, image_id=env.image_id,
                      base_image=env.base_image, python_version=env.python_version,
                      requirements=[f"{w.name}=={w.version}" for w in env.wheels], license=v.license.spdx,
                      provider=entry.identification.provider.value, scope=scope, verification_id=v.verification_id,
                      requested_by_task=task_id, reason=reason[:500])


def _summary(entry: RegistryEntry, draft: GrantDraft) -> list[str]:
    s, v = draft.scope, entry.verification
    warn = [f"{f.rule} {f.path}:{f.line}" for f in v.findings if f.severity == "warning"][:5]
    lines = [
        f"Repository: {entry.identification.url} @ {entry.commit[:12]} ({draft.provider}; "
        f"paper: {(entry.identification.paper.title if entry.identification.paper else None) or 'unknown'})",
        f"License: {draft.license} ({v.license.category}) - {v.license.reasons[0] if v.license.reasons else ''}",
        f"Environment: {draft.base_image.split('@')[0]} python {draft.python_version}, "
        f"{len(draft.requirements)} locked wheel(s): {', '.join(draft.requirements[:8])}"
        + (" ..." if len(draft.requirements) > 8 else ""),
        f"Verification {v.verification_id}: {v.status}; tests "
        + ", ".join(f"{t.name}={'ok' if t.passed else 'fail'}" for t in v.tests[:8]),
        f"Purpose: {', '.join(s.purposes)} - {s.purpose_detail[:200]}",
        f"Research repositories: {', '.join(s.research_repositories) or 'any registered'}",
        f"File access: per-job research checkout ({s.file_access.job_checkout}); data (ro): "
        f"{', '.join(s.file_access.data_paths) or 'none'}; no home, secrets or live repository",
        f"Network: {s.network}",
        f"Limits: {s.max_cpus:g} CPU, {s.max_memory_gb:g} GB, {s.max_walltime_minutes} min per run, "
        f"{s.max_total_cpu_hours:g} CPU-h total; research patches {'allowed' if s.allow_research_patches else 'not allowed'}",
    ]
    if warn:
        lines.append("Scan warnings: " + "; ".join(warn))
    if v.unverified:
        lines.append("Unverified: " + "; ".join(x[:120] for x in v.unverified[:4]))
    return lines


def request_grant(store, entry: RegistryEntry, draft: GrantDraft, *, notifier=None, diff: list[str] = ()) -> ApprovalRequest:
    """Idempotent per approval hash: a pending, rejected or revision-requested request for the same draft is
    returned instead of asking again (a human's "no" is not re-asked automatically)."""
    h = draft.approval_hash()
    same = [ApprovalRequest.model_validate(r) for r in store.find(db.approval_requests)
            if r.get("kind") == ApprovalKind.EXTERNAL_CODE.value and r.get("spec_hash") == h]
    for req in reversed(same):
        if req.status in (ApprovalStatus.PENDING, ApprovalStatus.REJECTED, ApprovalStatus.REVISION_REQUESTED):
            return req
    key = f"external_code:{h}:{len(same)}"
    req = ApprovalRequest(kind=ApprovalKind.EXTERNAL_CODE, task_id=_task(draft), root_task_id=_task(draft),
                          spec_hash=h, title=f"Run external code {entry.name}@{entry.commit[:12]} in the research environment",
                          what_changes=_summary(entry, draft), diff_from_original=list(diff),
                          budget_summary=f"{draft.scope.max_total_cpu_hours:g} CPU-h total, "
                                         f"{draft.scope.max_cpus:g} CPU / {draft.scope.max_memory_gb:g} GB per run",
                          payload={"grant_draft": draft.model_dump(mode="json"), "_dedupe": key})
    if store.insert_new(db.approval_requests, req.request_id, req.model_dump(mode="json"), task_id=req.task_id,
                        status=req.status.value, dedupe_key=key):
        entry.event("autopilot", "grant_requested", f"approval request {req.request_id}")
        registry.save_entry(store, entry)
        _notify(store, req, notifier)
    return req


def request_license_decision(store, entry: RegistryEntry, *, notifier=None) -> ApprovalRequest:
    lic = entry.verification.license
    h = stable_hash({"entry": entry.entry_id, "detected": lic.detected, "use": lic.intended_use})
    key = f"external_license:{h}"
    for r in store.find(db.approval_requests, dedupe_key=key):
        return ApprovalRequest.model_validate(r)
    req = ApprovalRequest(kind=ApprovalKind.EXTERNAL_LICENSE, task_id=f"external:{entry.name}"[:64],
                          root_task_id=f"external:{entry.name}"[:64], spec_hash=h,
                          title=f"License decision for {entry.name}@{entry.commit[:12]} ({lic.intended_use})",
                          what_changes=[f"Repository: {entry.identification.url}",
                                        *[f"detected {k}: {v}" for k, v in list(lic.detected.items())[:6]],
                                        *[f"reason: {r}" for r in lic.reasons],
                                        "approve = the terms permit this use; reject = do not use this repository"],
                          diff_from_original=[], budget_summary="no execution is requested by this decision",
                          payload={"entry_id": entry.entry_id, "_dedupe": key})
    if store.insert_new(db.approval_requests, req.request_id, req.model_dump(mode="json"), task_id=req.task_id,
                        status=req.status.value, dedupe_key=key):
        _notify(store, req, notifier)
    return req


def withdraw_license_requests(store, entry_id: str, note: str) -> None:
    for r in store.find(db.approval_requests, status=ApprovalStatus.PENDING.value):
        if r.get("kind") == ApprovalKind.EXTERNAL_LICENSE.value and r.get("payload", {}).get("entry_id") == entry_id:
            req = ApprovalRequest.model_validate(r)
            req.status = ApprovalStatus.STALE
            req.payload["withdrawn"] = note[:300]
            store.put(db.approval_requests, req.request_id, req.model_dump(mode="json"), task_id=req.task_id,
                      status=req.status.value, dedupe_key=req.payload.get("_dedupe", req.request_id))
            withdraw_undelivered(store, lambda ev: ev.get("approval_request_id") == req.request_id)
    withdraw_undelivered(store, lambda ev: ev.get("dedupe_key") == f"external:{entry_id}:needs_human")


def withdraw_undelivered(store, match) -> None:
    """Drop queued (not yet delivered) Discord messages about a question that no longer exists."""
    for r in store.find(db.notifications_sent):
        ev = r.get("event") or {}
        if r.get("state") in ("pending", "failed") and match(ev):
            store.put(db.notifications_sent, r["dedupe_key"],
                      {**r, "state": "sent", "message_id": None, "last_error": "withdrawn before delivery"})


def _task(draft: GrantDraft) -> str:
    return (draft.requested_by_task or f"external:{draft.name}")[:64]


def _notify(store, req: ApprovalRequest, notifier) -> None:
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    try:
        notify_once(store, NotificationEvent(
            kind=EventKind.APPROVAL_REQUIRED, flow_name="external-code", flow_run_name=req.task_id,
            state_name=f"{req.kind.value}:{req.status.value}",
            details=[f"Approval: {req.title}", *req.what_changes[:12], *[f"diff: {d}" for d in req.diff_from_original[:5]],
                     f"budget: {req.budget_summary}"],
            dedupe_key=f"approval:{req.request_id}", approval_request_id=req.request_id), notifier)
    except Exception:  # the request is durable; delivery is retried by the notifier outbox
        pass


# ---------------------------------------------------------------- decisions -> grants


def current_hash(store, req: ApprovalRequest) -> str:
    """The hash the request must still match (a changed / failed entry makes the request stale)."""
    if req.kind is ApprovalKind.EXTERNAL_LICENSE:
        entry = registry.get_entry(store, req.payload["entry_id"])
        if entry is None or entry.verification is None:
            return "unavailable"
        lic = entry.verification.license
        return stable_hash({"entry": entry.entry_id, "detected": lic.detected, "use": lic.intended_use})
    draft = GrantDraft.model_validate(req.payload["grant_draft"])
    entry = registry.get_entry(store, draft.entry_id)
    if entry is None or entry.state not in (EntryState.REGISTERED, EntryState.SUPERSEDED):
        return "entry-not-registered"
    fresh = draft_for(entry, draft.scope, task_id=draft.requested_by_task, reason=draft.reason)
    return fresh.approval_hash()


def process_decisions(store, *, catalog=None, notifier=None, on_license=None) -> list[dict]:
    """Consume verified Discord decisions on external-code / license requests (idempotent)."""
    ctl = ApprovalController(store, None, None, mode="discord")
    out = []
    for req in ctl.pending():
        if req.kind not in KINDS:
            continue
        res = ctl.check(req, _NoIssue(), [], current_hash(store, req))
        item = {"request_id": req.request_id, "kind": req.kind.value, "status": res.status.value, "note": res.note}
        if res.decision is not None:
            item.update(_apply(store, req, res.decision, catalog, on_license))
        out.append(item)
    return out


def _apply(store, req: ApprovalRequest, decision, catalog, on_license) -> dict:
    if req.kind is ApprovalKind.EXTERNAL_LICENSE:
        entry = registry.get_entry(store, req.payload["entry_id"])
        entry.verification.license.human_decision = {"decision": decision.decision, "actor": decision.actor,
                                                     "at": decision.decided_at.isoformat(), "comment": decision.comment}
        entry.event(decision.actor, f"license_{decision.decision}", decision.comment)
        rec = GrantDecisionRecord(request_id=req.request_id, entry_id=entry.entry_id, approval_hash=req.spec_hash,
                                  decision=f"license_{decision.decision}", actor=decision.actor,
                                  channel=decision.channel, comment=decision.comment)
        registry.save_decision(store, rec, catalog)
        if decision.decision != "approve":
            entry.state = EntryState.FAILED
            entry.verification.failure_reasons.append(f"license use not approved by {decision.actor}")
        registry.save_entry(store, entry, catalog)
        if decision.decision == "approve" and on_license is not None:
            on_license(entry)
        return {"entry_id": entry.entry_id}
    draft = GrantDraft.model_validate(req.payload["grant_draft"])
    grant = None
    if decision.decision == "approve":
        grant = ExecutionGrant(draft=draft, approval_hash=req.spec_hash, approved_by=decision.actor,
                               approval_request_id=req.request_id, channel=decision.channel)
        registry.save_grant(store, grant, catalog)
    registry.save_decision(store, GrantDecisionRecord(
        request_id=req.request_id, entry_id=draft.entry_id, approval_hash=req.spec_hash, decision=decision.decision,
        actor=decision.actor, channel=decision.channel, comment=decision.comment,
        grant_id=grant.grant_id if grant else None), catalog)
    entry = registry.get_entry(store, draft.entry_id)
    if entry is not None:
        entry.event(decision.actor, f"execution_{decision.decision}",
                    f"request {req.request_id}" + (f" -> {grant.grant_id}" if grant else "") + f" {decision.comment}")
        registry.save_entry(store, entry, catalog)
    return {"grant_id": grant.grant_id if grant else None, "decision": decision.decision}


def revoke(store, grant_id: str, actor: str, reason: str, catalog=None) -> ExecutionGrant:
    raw = store.get(db.external_grants, grant_id)
    if raw is None:
        raise ValueError(f"unknown grant {grant_id}")
    g = ExecutionGrant.model_validate(raw)
    g.status, g.revoked_reason = GrantStatus.REVOKED, f"{actor}: {reason}"[:300]
    registry.save_grant(store, g, catalog)
    return g


# ---------------------------------------------------------------- runtime gate


def _within(path: str, roots: list[str]) -> bool:
    p = os.path.realpath(os.path.expanduser(path))
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in (os.path.realpath(os.path.expanduser(x)) for x in roots))


def scope_problems(g: ExecutionGrant, req: ExecutionRequest, used_cpu_hours: float) -> list[str]:
    s = g.draft.scope
    out = []
    if req.purpose not in s.purposes:
        out.append(f"purpose {req.purpose} not approved (approved: {', '.join(s.purposes)})")
    if s.research_repositories and req.research_repository not in s.research_repositories:
        out.append(f"research repository {req.research_repository} not approved")
    if req.network != "none" and s.network != "unrestricted":
        out.append(f"network {req.network} exceeds approved {s.network}")
    if req.file_access.job_checkout == "rw" and s.file_access.job_checkout != "rw":
        out.append("write access to the job checkout not approved")
    extra = [d for d in req.file_access.data_paths if not _within(d, s.file_access.data_paths)]
    if extra:
        out.append(f"data paths not approved: {extra}")
    if req.cpus > s.max_cpus:
        out.append(f"{req.cpus:g} CPUs > approved {s.max_cpus:g}")
    if req.memory_gb > s.max_memory_gb:
        out.append(f"{req.memory_gb:g} GB > approved {s.max_memory_gb:g} GB")
    if req.walltime_minutes > s.max_walltime_minutes:
        out.append(f"{req.walltime_minutes} min > approved {s.max_walltime_minutes} min per run")
    need = used_cpu_hours + req.cpus * req.walltime_minutes / 60
    if need > s.max_total_cpu_hours:
        out.append(f"compute budget: {used_cpu_hours:.2f} used + {req.cpus * req.walltime_minutes / 60:.2f} requested "
                   f"> approved {s.max_total_cpu_hours:g} CPU-h")
    if req.patches and not s.allow_research_patches:
        out.append("research patches to the external code not approved")
    if g.expires_at < utcnow():
        out.append("grant expired")
    return out


def widened_scope(base: ExecutionScope | None, req: ExecutionRequest) -> ExecutionScope:
    """Smallest scope covering both the existing grant and the new request (for a re-approval draft)."""
    b = base or ExecutionScope(purposes=[], max_cpus=0, max_memory_gb=0, max_walltime_minutes=0,
                               max_total_cpu_hours=0, allow_research_patches=False,
                               file_access=FileAccess(job_checkout="ro"))
    walltime = max(b.max_walltime_minutes, req.walltime_minutes)
    return ExecutionScope(
        purposes=sorted(set(b.purposes) | {req.purpose}),
        purpose_detail=req.purpose_detail or b.purpose_detail,
        research_repositories=sorted(set(b.research_repositories) | {req.research_repository}),
        file_access=FileAccess(job_checkout="rw" if "rw" in (b.file_access.job_checkout, req.file_access.job_checkout) else "ro",
                               data_paths=sorted(set(b.file_access.data_paths) | set(req.file_access.data_paths))),
        network="unrestricted" if "unrestricted" in (b.network, req.network) else "none",
        max_cpus=max(b.max_cpus, req.cpus), max_memory_gb=max(b.max_memory_gb, req.memory_gb),
        max_walltime_minutes=walltime,
        max_total_cpu_hours=max(b.max_total_cpu_hours, round(max(req.cpus, 1) * walltime / 60 * 10, 2)),
        allow_research_patches=b.allow_research_patches or bool(req.patches))


def authorize(store, req: ExecutionRequest, *, docker=None) -> GateResult:
    entry = registry.find_entry(store, req.name, req.commit)
    if entry is None:
        return GateResult(allowed=False, reasons=[f"{req.name}@{req.commit or 'current'} is not in the Registry"])
    if entry.state not in (EntryState.REGISTERED, EntryState.SUPERSEDED) or entry.verification is None \
            or entry.verification.status != "passed":
        return GateResult(allowed=False, entry=entry,
                          reasons=[f"{entry.entry_id} is {entry.state.value}; only verified, registered code can run"])
    v = entry.verification
    reasons = []
    if not Path(entry.source_dir).is_dir() or tree_sha256(Path(entry.source_dir)) != v.integrity.tree_sha256:
        reasons.append("local source no longer matches the verified tree (changed or missing): re-verification needed")
    if docker is not None and v.environment and not docker.image_exists(v.environment.image_id):
        reasons.append("the verified execution image is missing: re-verification needed")
    if reasons:
        return GateResult(allowed=False, entry=entry, reasons=reasons)
    candidates = [g for g in registry.grants(store, name=entry.name, status=GrantStatus.ACTIVE)]
    best: tuple[list[str], ExecutionGrant | None] = (["no execution grant for this commit: a human must approve "
                                                      "the first execution"], None)
    for g in candidates:
        d = g.draft
        if (d.entry_id, d.commit, d.tree_sha256, d.lock_sha256, d.image_id) != (
                entry.entry_id, entry.commit, v.integrity.tree_sha256, v.environment.lock_sha256, v.environment.image_id):
            if best[1] is None:
                best = ([f"grant {g.grant_id} covers commit {d.commit[:12]}, not {entry.commit[:12]}: "
                         "a changed commit / dependency set needs re-approval"], None)
            continue
        used = sum(x.cpu_hours for x in registry.executions(store, grant_id=g.grant_id))
        problems = scope_problems(g, req, used)
        if not problems:
            return GateResult(allowed=True, grant=g, entry=entry)
        if best[1] is None or len(problems) < len(best[0]):
            best = (problems, g)
    problems, g = best
    draft = draft_for(entry, widened_scope(g.draft.scope if g else None, req), task_id=req.task_id,
                      reason="; ".join(problems))
    return GateResult(allowed=False, entry=entry, grant=g, reasons=problems, draft=draft)


# ---------------------------------------------------------------- research modifications


def prepare_code(entry: RegistryEntry, patches: list[str]) -> tuple[Path, list[dict], str]:
    """Code dir to mount: the verified tree itself, or a patched copy (research modifications are tracked)."""
    src = Path(entry.source_dir)
    if not patches:
        return src, [], entry.verification.integrity.tree_sha256
    import hashlib
    recs = [{"path": p, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in patches]
    key = stable_hash([r["sha256"] for r in recs])[:16]
    dest = ext_root() / "patched" / f"{entry.entry_id}+{key}"
    if not dest.exists():
        with tempfile.TemporaryDirectory(dir=ext_root()) as tmp:
            work = Path(tmp) / "code"
            subprocess.run(["cp", "-R", str(src), str(work)], check=True)
            subprocess.run(["chmod", "-R", "u+w", str(work)], check=True)
            for p in patches:
                r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(Path(p).resolve())], cwd=work,
                                   capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
                if r.returncode != 0:
                    raise ValueError(f"patch {Path(p).name} does not apply to {entry.entry_id}: {r.stderr[-300:]}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            work.rename(dest)
    return dest, recs, tree_sha256(dest)


def record_start(store, gate: GateResult, req: ExecutionRequest, *, argv: list[str], code_sha256: str,
                 patches: list[dict], job_key: str | None = None, experiment_run_id: str | None = None,
                 experiment_id: str | None = None, catalog=None) -> ExecutionRecord:
    rec = ExecutionRecord(grant_id=gate.grant.grant_id, entry_id=gate.entry.entry_id, name=gate.entry.name,
                          commit=gate.entry.commit, image_id=gate.entry.verification.environment.image_id,
                          task_id=req.task_id, research_repository=req.research_repository, job_key=job_key,
                          experiment_run_id=experiment_run_id, experiment_id=experiment_id, argv=argv,
                          patches=patches, code_sha256=code_sha256,
                          requested=req.model_dump(mode="json", include={"purpose", "purpose_detail", "network",
                                                                          "file_access", "cpus", "memory_gb",
                                                                          "walltime_minutes"}))
    registry.save_execution(store, rec, catalog)
    return rec


def record_finish(store, rec: ExecutionRecord, *, exit_code: int | None, wall_seconds: float, cpus: float,
                  detail: str = "", catalog=None) -> ExecutionRecord:
    rec.finished_at = utcnow()
    rec.exit_code = exit_code
    rec.cpu_hours = round(cpus * wall_seconds / 3600, 6)
    rec.outcome = "succeeded" if exit_code == 0 else "failed"
    rec.detail = detail[:500]
    registry.save_execution(store, rec, catalog)
    return rec
