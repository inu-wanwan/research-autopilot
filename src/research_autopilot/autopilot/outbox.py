"""Outbox for distributed Notion / Linear writes.

Each item advances through PENDING -> NOTION_CREATED -> LINEAR_CREATED -> LINKED -> DONE. Before every
create, the target is searched by the ``[ap:<key8>]`` title marker, so a crash after a remote write but
before the local state update adopts the existing object instead of creating a duplicate. Failures retry
with backoff; after ``max_attempts`` the item goes to DEAD (manual intervention, reported).
"""

import hashlib
from dataclasses import dataclass

from research_autopilot.autopilot.models import OutboxItem, OutboxState
from research_autopilot.models import utcnow
from research_autopilot.storage import db
from research_autopilot.storage.db import Store
from research_autopilot.tasks.notion import PROP_TITLE
from research_autopilot.tasks.notion_writer import NOTES_TITLE


def marker(key: str) -> str:
    return f"[ap:{hashlib.sha256(key.encode()).hexdigest()[:10]}]"


@dataclass
class Outbox:
    store: Store
    notion: object  # NotionWriter (or fake)
    linear: object  # LinearWriter (or fake)
    linear_ops: object  # LinearOps (guards)
    max_attempts: int = 5

    def enqueue(self, item: OutboxItem) -> OutboxItem:
        """Idempotent: a second enqueue with the same key returns the existing item."""
        if not self.store.insert_new(db.outbox, item.key, item.model_dump(mode="json"), state=item.state.value):
            return OutboxItem.model_validate(self.store.get(db.outbox, item.key))
        return item

    def get(self, key: str) -> OutboxItem | None:
        rec = self.store.get(db.outbox, key)
        return OutboxItem.model_validate(rec) if rec else None

    def pending(self) -> list[OutboxItem]:
        return [OutboxItem.model_validate(r) for r in self.store.find(
            db.outbox, state=[s.value for s in OutboxState if s not in (OutboxState.DONE, OutboxState.DEAD)])]

    def process(self, item: OutboxItem) -> OutboxItem:
        try:
            if item.kind == "proposal_task":
                self._proposal(item)
            elif item.kind == "notes":
                self._notes(item)
            elif item.kind == "linear_comment":
                self._comment(item)
            item.last_error = None
        except Exception as exc:  # noqa: BLE001 - recorded, retried, then dead-lettered
            item.attempts += 1
            item.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if item.attempts >= self.max_attempts:
                item.state = OutboxState.DEAD
        self._save(item)
        return item

    # ---------------------------------------------------------------- kinds

    def _proposal(self, item: OutboxItem) -> None:
        p = item.payload
        mk = marker(item.key)
        title = f"{p['title']} {mk}"
        if item.state is OutboxState.PENDING:
            found = self.notion.find_by_marker(self.notion.tasks_ds, PROP_TITLE, mk)
            item.notion_page_id = found[0] if found else self.notion.create_task_page(
                title, p["blocks"], pending=p["pending"], category=p.get("category"),
                parent_page_id=p.get("parent_page_id"))[0]
            item.state = OutboxState.NOTION_CREATED
            self._save(item)
        if item.state is OutboxState.NOTION_CREATED:
            found = self.linear.find_by_marker(mk)
            if found:
                item.linear_issue_id, item.linear_identifier = found[0]["id"], found[0]["identifier"]
            else:
                parent = self.linear.get_issue(p["parent_task_id"])
                self.linear_ops._guard(parent)  # test-only / write mode applies to the parent chain
                states = self.linear.team_states(parent.team_id)
                state_id = states[p["linear_state"]][0]
                desc = p["description"] + f"\n\nNotion SSOT: {self.notion.page_url(item.notion_page_id)}"
                item.linear_issue_id, item.linear_identifier, _ = self.linear.create_issue(
                    parent.team_id, title, desc, state_id, self.linear.label_ids(p.get("labels", [])))
                self.store.record_linear_write(item.linear_identifier, p["linear_state"], f"created for {item.key}")
            item.state = OutboxState.LINEAR_CREATED
            self._save(item)
        if item.state is OutboxState.LINEAR_CREATED:
            self.notion.set_task_links(item.notion_page_id, linear_identifier=item.linear_identifier)
            item.state = OutboxState.LINKED
            self._save(item)
        if item.state is OutboxState.LINKED:
            item.state = OutboxState.DONE

    def _notes(self, item: OutboxItem) -> None:
        mk = marker(item.key)
        found = self.notion.find_by_marker(self.notion.notes_ds, NOTES_TITLE, mk)
        item.notion_page_id = found[0] if found else self.notion.create_note(
            f"{item.payload['title']} {mk}", item.payload["blocks"])[0]
        item.state = OutboxState.DONE

    def _comment(self, item: OutboxItem) -> None:
        issue = self.linear.get_issue(item.payload["task_id"])
        mk = marker(item.key)
        item.linear_issue_id = issue.id
        self.linear_ops.comment(issue, item.payload["body"] + f"\n\n{mk}")
        item.state = OutboxState.DONE

    def _save(self, item: OutboxItem) -> None:
        item.updated_at = utcnow()
        self.store.put(db.outbox, item.key, item.model_dump(mode="json"), state=item.state.value)
