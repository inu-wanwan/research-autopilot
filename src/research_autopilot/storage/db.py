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
