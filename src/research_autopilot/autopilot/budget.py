"""Cumulative budget usage of a research chain (root task + all follow-up tasks).

API cost: Phase 2 plan runs (incl. Phase 8 literature research) + Phase 3 known CLI cost + Phase 4 LLM cost. Calls with unknown
price are counted separately. Compute: allocated CPUs x wall time of every Phase 4 job.
Nothing is reset by retries / fallbacks / repairs.
"""

from dataclasses import dataclass

from research_autopilot.storage.db import Store


@dataclass
class Usage:
    api_usd: float = 0.0
    cpu_hours: float = 0.0
    unknown_api_cost_calls: int = 0


def chain_usage(store: Store, runs: list[dict]) -> Usage:
    u = Usage()
    for run in runs:
        if run.get("plan_run_id"):
            rec = store.get_run(run["plan_run_id"])
            if rec and rec.cost:
                if rec.cost.estimated_cost_usd is None and rec.llm_calls:
                    u.unknown_api_cost_calls += len(rec.llm_calls)
                    u.api_usd += rec.literature_cost_usd  # Phase 8 research cost is known even then
                else:
                    u.api_usd += rec.cost.estimated_cost_usd or 0.0
        for rid in run.get("phase3_run_ids", []):
            m = store.get_implementation(rid)
            if m:
                u.api_usd += m.cost_usd_known  # Codex via subscription has no API cost
        for eid in run.get("experiment_run_ids", []):
            e = store.get_experiment(eid)
            if not e:
                continue
            u.api_usd += e.llm_cost_known
            u.unknown_api_cost_calls += sum(1 for x in e.llm_usage if x.cost_usd is None and not x.error)
            for j in e.jobs:
                st = j.status
                if j.allocation and st and st.started_at and st.finished_at:
                    u.cpu_hours += j.allocation.cpu * (st.finished_at - st.started_at).total_seconds() / 3600
    u.api_usd = round(u.api_usd, 6)
    u.cpu_hours = round(u.cpu_hours, 6)
    return u
