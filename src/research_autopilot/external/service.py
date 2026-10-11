"""Onboarding pipeline: identify -> isolated fetch at a fixed commit -> license -> verification -> registration.

Every step is automatic except the decisions the spec reserves for a human (unclear license terms here; the
first execution grant in ``grants``).  Re-running ``onboard`` for the same commit is idempotent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from research_autopilot.external import github, grants, registry
from research_autopilot.external import license as license_mod
from research_autopilot.external import verify as verify_mod
from research_autopilot.external.models import (EntryState, PaperRef, RegistryEntry, SandboxLimits, Verification)
from research_autopilot.models import utcnow

UPDATE_COOLDOWN = timedelta(hours=24)


@dataclass
class ExternalDeps:
    store: Any
    net: Any                     # literature.net.Net (allowlisted, credential-free HTTPS)
    docker: Any                  # sandbox.Docker
    llm: Any = None              # PricedLlm-like callable for the test planner (None: import checks only)
    catalog: Any = None          # catalog.service.CatalogContext for the Phase 7 mirror
    notifier: Any = None
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    intended_use: str = "internal_research"
    budget_usd: float = 1.0      # LLM budget per onboarding (test planning)


def entry_id_for(full_name: str, commit: str) -> str:
    return "ext-" + re.sub(r"[^a-z0-9]+", "-", full_name.lower()).strip("-") + "@" + commit[:12]


def onboard(deps: ExternalDeps, name: str, *, commit: str | None = None, paper: PaperRef | None = None,
            paper_text: str | None = None, actor: str = "autopilot", retry_failed: bool = False) -> RegistryEntry:
    deps.store.init_schema()
    ident = github.identify(deps.net, name, commit=commit, paper=paper, paper_text=paper_text)
    eid = entry_id_for(ident.full_name, ident.commit)
    old = registry.get_entry(deps.store, eid)
    if old is not None and old.state is EntryState.NEEDS_HUMAN and not (old.verification.license.human_decision or {}):
        return _reassess_license(deps, old, actor)
    if old is not None and (old.state in (EntryState.REGISTERED, EntryState.SUPERSEDED, EntryState.NEEDS_HUMAN)
                            or (old.state is EntryState.FAILED and not retry_failed)):
        return old
    entry = RegistryEntry(entry_id=eid, name=ident.full_name, commit=ident.commit, identification=ident, source_dir="")
    if old is not None:
        entry.history = old.history
    entry.event(actor, "identified", f"provider={ident.provider.value}; " + "; ".join(ident.provider_evidence)[:300])
    try:
        src, integ, origin = github.fetch(deps.net, ident)
    except Exception as exc:
        entry.state = EntryState.FAILED
        entry.verification = Verification(limits=deps.limits, failure_reasons=[f"fetch failed: {type(exc).__name__}: {exc}"[:300]])
        entry.event(actor, "fetch_failed", str(exc))
        registry.save_entry(deps.store, entry, deps.catalog)
        return entry
    entry.source_dir, entry.origin_url, entry.retrieved_at = str(src), origin, utcnow()
    entry.verification = Verification(limits=deps.limits, integrity=integ)
    entry.event(actor, "fetched", f"{integ.verified_blobs}/{integ.files} file(s) match the commit's Git blobs")
    if not integ.ok:
        entry.state = EntryState.FAILED
        entry.verification.failure_reasons.append("source does not match the fixed commit: "
                                                  + "; ".join(integ.mismatches[:5]))
        return _save(deps, entry)
    entry.state = EntryState.VERIFYING
    lic = license_mod.assess(Path(src), ident.github_license, deps.intended_use)
    entry.verification.license = lic
    entry.event(actor, "license", f"{lic.spdx or 'unknown'} -> {lic.decision}: {'; '.join(lic.reasons)[:300]}")
    if lic.decision == "incompatible":
        entry.state = EntryState.FAILED
        entry.verification.failure_reasons.append("license terms do not permit the intended use")
        return _save(deps, entry)
    if lic.decision == "needs_human":
        entry.state = EntryState.NEEDS_HUMAN
        _save(deps, entry)
        grants.request_license_decision(deps.store, entry, notifier=deps.notifier)
        return entry
    return verify_and_register(deps, entry, actor)


def _reassess_license(deps: ExternalDeps, entry: RegistryEntry, actor: str) -> RegistryEntry:
    """A pending license question is re-evaluated (the detector or policy may have improved); when the terms are
    now clear the pending human request is withdrawn (stale) and verification continues."""
    lic = license_mod.assess(Path(entry.source_dir), entry.identification.github_license, deps.intended_use)
    if lic.decision != "auto_ok":
        return entry
    entry.verification.license = lic
    entry.event(actor, "license", f"re-assessed: {lic.spdx} -> auto_ok; pending human request withdrawn")
    grants.withdraw_license_requests(deps.store, entry.entry_id, "license re-assessed automatically: " + lic.reasons[0])
    return verify_and_register(deps, entry, actor)


def verify_and_register(deps: ExternalDeps, entry: RegistryEntry, actor: str = "autopilot") -> RegistryEntry:
    entry.state = EntryState.VERIFYING
    registry.save_entry(deps.store, entry, deps.catalog)
    slug = re.sub(r"[^a-z0-9._-]+", "-", entry.name.lower())
    work = github.ext_root() / "work"
    work.mkdir(parents=True, exist_ok=True)
    if deps.llm is not None and hasattr(deps.llm, "budget_usd"):
        deps.llm.budget_usd = getattr(deps.llm, "spent", 0.0) + deps.budget_usd  # fresh budget per onboarding
    verify_mod.verify(Path(entry.source_dir), entry.verification, docker=deps.docker, llm=deps.llm,
                      image_tag=f"ra-ext/{slug}:{entry.commit[:12]}", work_root=work)
    v = entry.verification
    entry.event(actor, "verified" if v.status == "passed" else "verification_failed",
                "; ".join(v.failure_reasons)[:400] or f"{len(v.tests)} sandbox run(s) passed")
    if v.status == "passed":
        entry.state = EntryState.VERIFIED
        registry.register(deps.store, entry, deps.catalog)
        grants.withdraw_undelivered(deps.store, lambda ev: ev.get("dedupe_key") == f"external:{entry.entry_id}:failed")
        _notify(deps, entry)
        return entry
    entry.state = EntryState.FAILED
    return _save(deps, entry)


def verify_patched(deps: ExternalDeps, entry: RegistryEntry, patches: list[str]) -> list[str]:
    """Re-verification of research-modified code before it is put to a human: the patches must apply to the
    verified tree and the modules must still import in the sandbox (same locked image, no network)."""
    if not patches:
        return []
    code, recs, sha = grants.prepare_code(entry, patches)
    v = entry.verification
    lines = [f"patched code {sha[:12]} from {', '.join(Path(r['path']).name + ':' + r['sha256'][:12] for r in recs)}"]
    for name, argv, wd in verify_mod.import_checks(v.survey, code):
        res = deps.docker.run_isolated(v.environment.image_id, code, argv, workdir=wd, python_dirs=v.survey.python_dirs,
                                       limits=deps.limits)
        lines.append(f"patched {name}: {'ok' if res['exit_code'] == 0 else 'FAILED ' + res['stderr'][-150:]}")
    return lines


def resume_after_license(deps: ExternalDeps, entry: RegistryEntry) -> RegistryEntry:
    if entry.state is EntryState.NEEDS_HUMAN:
        return verify_and_register(deps, entry, actor="autopilot")
    return entry


def check_update(deps: ExternalDeps, name: str) -> RegistryEntry | None:
    """A newer default-branch commit is verified on its own; the current entry stays current unless it passes."""
    cur = registry.current_entry(deps.store, name)
    if cur is None:
        return None
    last = max((h.at for h in cur.history if h.action in ("update_checked", "registered")), default=cur.retrieved_at)
    if utcnow() - last < UPDATE_COOLDOWN:
        return None
    ident = github.identify(deps.net, name, paper=cur.identification.paper)
    cur.event("autopilot", "update_checked", f"default branch at {ident.commit[:12]}")
    registry.save_entry(deps.store, cur, deps.catalog)
    if ident.commit == cur.commit:
        return None
    return onboard(deps, name, commit=ident.commit, paper=cur.identification.paper)


def _save(deps: ExternalDeps, entry: RegistryEntry) -> RegistryEntry:
    if entry.verification is not None and entry.verification.finished_at is None:
        entry.verification.finished_at = utcnow()
    registry.save_entry(deps.store, entry, deps.catalog)
    _notify(deps, entry)
    return entry


def _notify(deps: ExternalDeps, entry: RegistryEntry) -> None:
    if entry.state is EntryState.NEEDS_HUMAN:
        return  # the license approval request itself is the notification
    from research_autopilot.notifications import notify_once
    from research_autopilot.notifications.events import EventKind, NotificationEvent
    kind = EventKind.PROPOSAL_GENERATED if entry.state is EntryState.REGISTERED else EventKind.UNRECOVERABLE_ERROR
    v = entry.verification
    details = [f"{entry.identification.url} @ {entry.commit[:12]}: {entry.state.value}",
               f"license {v.license.spdx if v else '?'}; tests "
               + (", ".join(f"{t.name}={'ok' if t.passed else 'fail'}" for t in v.tests[:6]) if v else "-")]
    if entry.state is EntryState.REGISTERED:
        details.append("Registered only: running it for research needs a human execution grant.")
    elif v and v.failure_reasons:
        details.append("; ".join(v.failure_reasons)[:300])
    try:
        failed = entry.state is EntryState.FAILED
        notify_once(deps.store, NotificationEvent(kind=kind, flow_name="external-onboarding", flow_run_name=entry.name,
                                                  details=details, dedupe_key=f"external:{entry.entry_id}:{entry.state.value}",
                                                  needs_human=failed, steps=_failed_steps(entry) if failed else []),
                    deps.notifier)
    except Exception:
        pass


def _failed_steps(entry: RegistryEntry) -> list[str]:
    """What a human does after a failed onboarding (Autopilot never retries a failed commit by itself)."""
    reasons = " ".join(entry.verification.failure_reasons if entry.verification else [])
    if "license" in reasons:
        fix = ("ライセンスがこの用途を認めていないため使えない。Notion の仕様で別の実装・リポジトリを指定するか、"
               "外部コードを使わない方針に直す")
    else:
        fix = ("原因（取得・整合性・サンドボックス検証・依存関係）を確認し、必要なら Notion の仕様で別の commit や"
               "リポジトリを指定する。同じ commit をやり直す場合は research-agent external onboard "
               f"{entry.name} --retry-failed")
    return ["失敗の理由（下記）を確認する", fix, "直したら研究タスクの Notion ページで Run Autopilot を押す"]


def deps_from_env(store, catalog=None, notifier=None) -> ExternalDeps:
    from research_autopilot.config import load_settings
    from research_autopilot.external.sandbox import Docker
    from research_autopilot.literature.net import from_env
    from research_autopilot.literature.researcher import PricedLlm
    s = load_settings()
    model = os.environ.get("RA_EXTERNAL_MODEL", "").strip() or s.reviewer_model
    budget = float(os.environ.get("RA_EXTERNAL_BUDGET_USD") or 1.0)
    limits = SandboxLimits(cpus=float(os.environ.get("RA_EXTERNAL_SANDBOX_CPUS") or 2),
                           memory_gb=float(os.environ.get("RA_EXTERNAL_SANDBOX_MEMORY_GB") or 4),
                           timeout_s=int(os.environ.get("RA_EXTERNAL_SANDBOX_TIMEOUT_S") or 1200),
                           disk_gb=float(os.environ.get("RA_EXTERNAL_SANDBOX_DISK_GB") or 2))
    if catalog is not None:
        registry.with_external(catalog)
    return ExternalDeps(store=store, net=from_env(), docker=Docker(),
                        llm=PricedLlm(model, s.model_pricing, budget, s.agents_tracing), catalog=catalog,
                        notifier=notifier, limits=limits, budget_usd=budget)


def optional_from_env(store, catalog=None, notifier=None) -> ExternalDeps | None:
    """Phase 9 deps, or None when disabled (RA_EXTERNAL_ENABLED=false) or the network contact is not configured."""
    if os.environ.get("RA_EXTERNAL_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return None
    if not os.environ.get("RA_LITERATURE_CONTACT_EMAIL", "").strip():
        return None
    if catalog is None:
        from research_autopilot.catalog.integration import context_from_env
        catalog = context_from_env(store)
    return deps_from_env(store, catalog, notifier)
