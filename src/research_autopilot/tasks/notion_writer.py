"""Notion writes for Phase 5: proposal task pages (Research Tasks) and result pages (Research Notes).

Never writes the Status property (owned by the Linear->Notion mirror). Pages carry an
``[ap:<key>]`` marker in the title so a crashed write can be found and adopted instead of duplicated.
"""

from collections.abc import Callable
from datetime import date

import httpx

from research_autopilot.tasks.http import RetryPolicy, request
from research_autopilot.tasks.notion import (API, NOTION_VERSION, PROP_CATEGORY, PROP_LINEAR_ID, PROP_PENDING, PROP_TITLE,
                                             PROP_RUN_REQUESTED, PROP_EXECUTION_STATUS, PROP_LAST_RUN, PROP_EXECUTION_ERROR)

NOTES_TITLE = "名前"
NOTES_CREATED = "作成日"
NOTES_STATUS = "ステータス"
NOTES_CATEGORY = "分類"
NOTES_ADOPTED = "採用"
NOTES_RESULT = "結果"
from research_autopilot.tasks.notion import PROP_PARENT
MAX_TEXT = 1900  # Notion rich_text limit is 2000 chars per element


def _rt(text: str) -> list[dict]:
    text = text or ""
    return [{"type": "text", "text": {"content": text[i:i + MAX_TEXT]}} for i in range(0, max(len(text), 1), MAX_TEXT)][:20]


def heading(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(text)}}


def table(headers: list[str], rows: list[list[str]]) -> dict:
    def row(cells: list[str]) -> dict:
        return {"object": "block", "type": "table_row", "table_row": {"cells": [_rt(cell) for cell in cells]}}

    return {"object": "block", "type": "table", "table": {
        "table_width": len(headers), "has_column_header": True, "has_row_header": False,
        "children": [row(headers), *[row(values) for values in rows]],
    }}


def toggle(title: str, children: list[dict]) -> dict:
    return {"object": "block", "type": "toggle", "toggle": {"rich_text": _rt(title), "children": children}}


def section(title: str, body: str | list[str]) -> list[dict]:
    if isinstance(body, list):
        return [heading(title), *[bullet(x) for x in body]] if body else [heading(title), paragraph("-")]
    return [heading(title), paragraph(body or "-")]


class NotionWriter:
    def __init__(self, token: str, tasks_data_source_id: str, notes_data_source_id: str, *,
                 transport: httpx.BaseTransport | None = None, retry: RetryPolicy | None = None,
                 on_retry: Callable[[str], None] | None = None) -> None:
        self._client = httpx.Client(base_url=API, timeout=30, transport=transport,
                                    headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION})
        self.tasks_ds = tasks_data_source_id
        self.notes_ds = notes_data_source_id
        self._retry = retry or RetryPolicy()
        self._on_retry = on_retry

    def _call(self, method: str, url: str, **kw) -> dict:
        return request(self._client, "notion", method, url, self._retry, self._on_retry, **kw).json()

    def find_by_marker(self, data_source: str, title_prop: str, marker: str) -> list[str]:
        res = self._call("POST", f"/data_sources/{data_source}/query",
                         json={"filter": {"property": title_prop, "title": {"contains": marker}}, "page_size": 5})
        return [r["id"] for r in res.get("results", [])]

    def create_task_page(self, title: str, blocks: list[dict], *, pending: bool, category: str | None,
                         parent_page_id: str | None) -> tuple[str, str]:
        props: dict = {PROP_TITLE: {"title": _rt(title)}, PROP_PENDING: {"checkbox": pending}}
        if category:
            props[PROP_CATEGORY] = {"select": {"name": category}}
        if parent_page_id:
            props[PROP_PARENT] = {"relation": [{"id": parent_page_id}]}
        page = self._call("POST", "/pages", json={"parent": {"type": "data_source_id", "data_source_id": self.tasks_ds},
                                                  "properties": props, "children": blocks[:100]})
        return page["id"], page.get("url", "")

    def set_task_links(self, page_id: str, linear_identifier: str | None = None, pending: bool | None = None) -> None:
        props: dict = {}
        if linear_identifier is not None:
            props[PROP_LINEAR_ID] = {"rich_text": _rt(linear_identifier)}
        if pending is not None:
            props[PROP_PENDING] = {"checkbox": pending}
        if props:
            self._call("PATCH", f"/pages/{page_id}", json={"properties": props})

    def project_execution(self, page_id: str, *, status: str, run_url: str | None = None,
                          error: str | None = None, acknowledge: bool = False) -> None:
        """Independent of the Linear Status mirror; only execution properties are patched."""
        props = {PROP_EXECUTION_STATUS: {"status": {"name": status}},
                 PROP_EXECUTION_ERROR: {"rich_text": _rt(error) if error else []}}
        if run_url is not None:
            props[PROP_LAST_RUN] = {"url": run_url}
        if acknowledge:
            props[PROP_RUN_REQUESTED] = {"checkbox": False}
        self._call("PATCH", f"/pages/{page_id}", json={"properties": props})

    def create_note(self, title: str, blocks: list[dict]) -> tuple[str, str]:
        props = {
            NOTES_TITLE: {"title": _rt(title)},
            NOTES_CREATED: {"date": {"start": date.today().isoformat()}},
            NOTES_STATUS: {"status": {"name": NOTES_ADOPTED}},
            NOTES_CATEGORY: {"multi_select": [{"name": NOTES_RESULT}]},
        }
        page = self._call("POST", "/pages", json={"parent": {"type": "data_source_id", "data_source_id": self.notes_ds},
                                                  "properties": props, "children": blocks[:100]})
        return page["id"], page.get("url", "")

    def page_url(self, page_id: str) -> str:
        return f"https://www.notion.so/{page_id.replace('-', '')}"
