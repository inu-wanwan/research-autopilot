"""SQLAlchemy Core storage.

PostgreSQL in production (a dedicated ``research_autopilot`` database on the
Phase 1 PostgreSQL server; Prefect's own database is never touched), SQLite
for local development and tests. Tables are created with ``init_schema``.
"""

from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from research_autopilot.models import LinearUpdateResult, PlanRunRecord, utcnow

metadata = sa.MetaData()

plan_runs = sa.Table(
    "plan_runs", metadata,
    sa.Column("plan_run_id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(64), index=True),
    sa.Column("flow_run_id", sa.String(36)),
    sa.Column("mode", sa.String(16), nullable=False),
    sa.Column("status", sa.String(32)),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("planner_model", sa.String(64)),
    sa.Column("reviewer_model", sa.String(64)),
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("estimated_cost_usd", sa.Float),
    sa.Column("cost_complete", sa.Boolean),
    sa.Column("record", sa.JSON, nullable=False),
)

task_leases = sa.Table(
    "task_leases", metadata,
    sa.Column("task_id", sa.String(64), primary_key=True),
    sa.Column("plan_run_id", sa.String(36), nullable=False),
    sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

linear_audit = sa.Table(
    "linear_status_audit", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("plan_run_id", sa.String(36), nullable=False, index=True),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("action", sa.String(32), nullable=False),
    sa.Column("from_state", sa.String(64)),
    sa.Column("to_state", sa.String(64)),
    sa.Column("error", sa.Text),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
)


implementation_runs = sa.Table(
    "implementation_runs", metadata,
    sa.Column("phase3_run_id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(64), index=True, nullable=False),
    sa.Column("phase2_plan_run_id", sa.String(36)),
    sa.Column("flow_run_id", sa.String(36)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("implementation_commit", sa.String(64)),
    sa.Column("parent_run_id", sa.String(36), index=True),
    sa.Column("failure_report_id", sa.String(64), unique=True),  # duplicate FailureReport guard
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("record", sa.JSON, nullable=False),
)


experiment_runs = sa.Table(
    "experiment_runs", metadata,
    sa.Column("experiment_run_id", sa.String(36), primary_key=True),
    sa.Column("task_id", sa.String(64), index=True, nullable=False),
    sa.Column("phase3_run_id", sa.String(36), index=True),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("record", sa.JSON, nullable=False),
)

experiment_jobs = sa.Table(  # persistent job ledger (idempotent submit, reconciliation)
    "experiment_jobs", metadata,
    sa.Column("job_key", sa.String(160), primary_key=True),
    sa.Column("experiment_run_id", sa.String(36), index=True, nullable=False),
    sa.Column("backend", sa.String(16), nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("external_id", sa.String(64)),
    sa.Column("run_dir", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("record", sa.JSON),
)

experiment_proposals = sa.Table(
    "experiment_proposals", metadata,
    sa.Column("proposal_id", sa.String(36), primary_key=True),
    sa.Column("dedupe_key", sa.String(64), unique=True, nullable=False),
    sa.Column("experiment_run_id", sa.String(36), index=True, nullable=False),
    sa.Column("task_id", sa.String(64), index=True, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("record", sa.JSON, nullable=False),
)


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; everything is stored in UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


class Store:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = sa.create_engine(url, future=True)

    def init_schema(self) -> None:
        metadata.create_all(self.engine)

    # -------------------------------------------------- leases (duplicate-run guard)

    def acquire_lease(self, task_id: str, plan_run_id: str, ttl_seconds: int) -> bool:
        """Atomically take the per-task lease. An expired lease is taken over."""
        now = utcnow()
        expires = now + timedelta(seconds=ttl_seconds)
        with self.engine.begin() as conn:
            conn.execute(task_leases.delete().where(
                task_leases.c.task_id == task_id, task_leases.c.expires_at < now))
        try:
            with self.engine.begin() as conn:
                conn.execute(task_leases.insert().values(
                    task_id=task_id, plan_run_id=plan_run_id, acquired_at=now, expires_at=expires))
            return True
        except IntegrityError:
            return False

    def reclaim_or_acquire_lease(self, key: str, owner_id: str, ttl_seconds: int) -> bool:
        """Like acquire_lease, but an existing lease held by the same owner (a
        resumed run) is refreshed instead of refused."""
        current = self.active_lease(key)
        if current and current["plan_run_id"] == owner_id:
            with self.engine.begin() as conn:
                conn.execute(task_leases.update().where(task_leases.c.task_id == key).values(
                    expires_at=utcnow() + timedelta(seconds=ttl_seconds)))
            return True
        return self.acquire_lease(key, owner_id, ttl_seconds)

    def active_lease(self, task_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(task_leases).where(task_leases.c.task_id == task_id)).mappings().first()
        if row and _aware(row["expires_at"]) > utcnow():
            return dict(row)
        return None

    def release_lease(self, task_id: str, plan_run_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(task_leases.delete().where(
                task_leases.c.task_id == task_id, task_leases.c.plan_run_id == plan_run_id))

    # -------------------------------------------------- run history

    def save_run(self, record: PlanRunRecord) -> None:
        cost = record.cost
        values = dict(
            task_id=record.task_id, flow_run_id=record.flow_run_id, mode=record.mode,
            status=record.decision.status.value if record.decision else None,
            started_at=record.started_at, finished_at=record.finished_at,
            planner_model=record.planner_model, reviewer_model=record.reviewer_model,
            input_tokens=cost.input_tokens if cost else None,
            output_tokens=cost.output_tokens if cost else None,
            estimated_cost_usd=cost.estimated_cost_usd if cost else None,
            cost_complete=cost.cost_complete if cost else None,
            record=record.model_dump(mode="json"),
        )
        with self.engine.begin() as conn:
            updated = conn.execute(plan_runs.update().where(
                plan_runs.c.plan_run_id == record.plan_run_id).values(**values)).rowcount
            if not updated:
                conn.execute(plan_runs.insert().values(plan_run_id=record.plan_run_id, **values))

    def get_run(self, plan_run_id: str) -> PlanRunRecord | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(plan_runs.c.record).where(
                plan_runs.c.plan_run_id == plan_run_id)).first()
        return PlanRunRecord.model_validate(row[0]) if row else None

    def list_runs(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        q = sa.select(*[c for c in plan_runs.c if c.name != "record"]).order_by(plan_runs.c.started_at.desc()).limit(limit)
        if task_id:
            q = q.where(plan_runs.c.task_id == task_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # -------------------------------------------------- Linear audit

    def audit_linear(self, plan_run_id: str, task_id: str, result: LinearUpdateResult) -> None:
        with self.engine.begin() as conn:
            conn.execute(linear_audit.insert().values(
                plan_run_id=plan_run_id, task_id=task_id, action=result.action,
                from_state=result.from_state, to_state=result.to_state, error=result.error, at=result.at))

    def linear_audit_rows(self, task_id: str | None = None) -> list[dict]:
        q = sa.select(linear_audit).order_by(linear_audit.c.id)
        if task_id:
            q = q.where(linear_audit.c.task_id == task_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # -------------------------------------------------- Phase 3 implementation runs

    def save_implementation(self, manifest) -> None:
        values = dict(task_id=manifest.task_id, phase2_plan_run_id=manifest.phase2_plan_run_id,
                      flow_run_id=manifest.flow_run_id, state=manifest.state.value,
                      implementation_commit=manifest.implementation_commit, parent_run_id=manifest.parent_run_id,
                      failure_report_id=manifest.failure_report_id, created_at=manifest.created_at,
                      updated_at=manifest.updated_at, record=manifest.model_dump(mode="json"))
        with self.engine.begin() as conn:
            updated = conn.execute(implementation_runs.update().where(
                implementation_runs.c.phase3_run_id == manifest.phase3_run_id).values(**values)).rowcount
            if not updated:
                conn.execute(implementation_runs.insert().values(phase3_run_id=manifest.phase3_run_id, **values))

    def get_implementation(self, run_id: str):
        from research_autopilot.implementation.models import ImplementationManifest

        with self.engine.connect() as conn:
            row = conn.execute(sa.select(implementation_runs.c.record).where(
                implementation_runs.c.phase3_run_id == run_id)).first()
        return ImplementationManifest.model_validate(row[0]) if row else None

    def list_implementations(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        q = sa.select(*[c for c in implementation_runs.c if c.name != "record"]).order_by(
            implementation_runs.c.created_at.desc()).limit(limit)
        if task_id:
            q = q.where(implementation_runs.c.task_id == task_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def find_implementation_by_failure_report(self, report_id: str):
        from research_autopilot.implementation.models import ImplementationManifest

        with self.engine.connect() as conn:
            row = conn.execute(sa.select(implementation_runs.c.record).where(
                implementation_runs.c.failure_report_id == report_id)).first()
        return ImplementationManifest.model_validate(row[0]) if row else None

    # -------------------------------------------------- Phase 4 experiments

    def save_experiment(self, manifest) -> None:
        values = dict(task_id=manifest.task_id, phase3_run_id=manifest.phase3_run_id, state=manifest.state.value,
                      created_at=manifest.created_at, updated_at=manifest.updated_at,
                      record=manifest.model_dump(mode="json"))
        with self.engine.begin() as conn:
            if not conn.execute(experiment_runs.update().where(
                    experiment_runs.c.experiment_run_id == manifest.experiment_run_id).values(**values)).rowcount:
                conn.execute(experiment_runs.insert().values(experiment_run_id=manifest.experiment_run_id, **values))

    def get_experiment(self, run_id: str):
        from research_autopilot.experiments.models import ExperimentManifest

        with self.engine.connect() as conn:
            row = conn.execute(sa.select(experiment_runs.c.record).where(
                experiment_runs.c.experiment_run_id == run_id)).first()
        return ExperimentManifest.model_validate(row[0]) if row else None

    def list_experiments(self, task_id: str | None = None, limit: int = 20) -> list[dict]:
        q = sa.select(*[c for c in experiment_runs.c if c.name != "record"]).order_by(
            experiment_runs.c.created_at.desc()).limit(limit)
        if task_id:
            q = q.where(experiment_runs.c.task_id == task_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def ledger_insert(self, job_key: str, run_id: str, backend: str, run_dir: str) -> bool:
        """Insert a SUBMITTING row; False if the key already exists (duplicate submit)."""
        now = utcnow()
        try:
            with self.engine.begin() as conn:
                conn.execute(experiment_jobs.insert().values(job_key=job_key, experiment_run_id=run_id, backend=backend,
                                                             state="submitting", run_dir=run_dir, created_at=now,
                                                             updated_at=now))
            return True
        except IntegrityError:
            return False

    def ledger_update(self, job_key: str, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(experiment_jobs.update().where(experiment_jobs.c.job_key == job_key).values(
                updated_at=utcnow(), **values))

    def ledger_get(self, job_key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(experiment_jobs).where(experiment_jobs.c.job_key == job_key)).mappings().first()
        return dict(row) if row else None

    def ledger_open(self, backend: str | None = None) -> list[dict]:
        q = sa.select(experiment_jobs).where(experiment_jobs.c.state.in_(["submitting", "queued", "running"]))
        if backend:
            q = q.where(experiment_jobs.c.backend == backend)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def ledger_history(self, experiment_id: str) -> list[dict]:
        """Usage of previous successful jobs of the same experiment (for estimates)."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(experiment_jobs.c.record).where(
                experiment_jobs.c.state == "succeeded")).all()
        return [r[0] for r in rows if r[0] and r[0].get("experiment_id") == experiment_id]

    def save_proposal(self, reviewed) -> bool:
        """Store a ReviewedExperimentProposal; False if an identical proposal already exists."""
        p = reviewed.proposal
        try:
            with self.engine.begin() as conn:
                conn.execute(experiment_proposals.insert().values(
                    proposal_id=p.proposal_id, dedupe_key=p.dedupe_key, experiment_run_id=p.source_experiment_run_id,
                    task_id=p.parent_task_id, created_at=p.created_at, record=reviewed.model_dump(mode="json")))
            return True
        except IntegrityError:
            return False

    def list_proposals(self, task_id: str | None = None) -> list[dict]:
        q = sa.select(experiment_proposals.c.record).order_by(experiment_proposals.c.created_at)
        if task_id:
            q = q.where(experiment_proposals.c.task_id == task_id)
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(q).all()]
