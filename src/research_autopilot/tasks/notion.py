"""Read-only Notion connector for the Research Tasks data source.

Uses Notion API version 2025-09-03 (data sources). Only GET / query
endpoints are called; the integration should have the "Read content"
capability only.
"""

import re
from collections.abc import Callable

import httpx

from research_autopilot.models import NotionTask, utcnow
from research_autopilot.tasks.errors import ConnectorResponseError
from research_autopilot.tasks.http import RetryPolicy, request
from research_autopilot.tasks.sections import split_sections

NOTION_VERSION = "2025-09-03"
API = "https://api.notion.com/v1"

# Property names in the Research Tasks data source.
PROP_TITLE = "Name"
PROP_STATUS = "Status"
PROP_PENDING = "Pending"
PROP_LINEAR_ID = "Linear Issue ID"
PROP_CATEGORY = "分類"
PROP_RUN_REQUESTED = "Run Requested"
PROP_EXECUTION_STATUS = "Execution Status"
PROP_LAST_RUN = "Last Run"
PROP_EXECUTION_ERROR = "Execution Error"

MAX_BLOCKS = 2000
MAX_DEPTH = 4

_HEX32 = re.compile(r"([0-9a-f]{32})(?:[?#/]|$)", re.I)


def normalize_page_id(value: str) -> str:
    """Accept a Notion URL, a 32-hex id or a dashed UUID; return a dashed UUID."""
    raw = value.strip().replace("-", "")
    m = _HEX32.search(raw) if len(raw) != 32 else None
    hex32 = m.group(1) if m else raw
    if not re.fullmatch(r"[0-9a-fA-F]{32}", hex32):
        raise ValueError(f"not a Notion page id: {value!r}")
    h = hex32.lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def notion_ids_in_text(text: str) -> list[str]:
    ids = []
    for url in re.findall(r"https?://(?:www\.|app\.)?notion\.(?:so|com)/[^\s)>\]]+", text or ""):
        path = url.split("?")[0].split("#")[0].replace("-", "")
        m = re.search(r"([0-9a-f]{32})$", path, re.I)
        if m:
            pid = normalize_page_id(m.group(1))
            if pid not in ids:
                ids.append(pid)
    return ids


class NotionClient:
    def __init__(self, token: str, data_source_id: str, *, transport: httpx.BaseTransport | None = None,
                 retry: RetryPolicy | None = None, on_retry: Callable[[str], None] | None = None,
                 timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=API, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
        )
        self.data_source_id = data_source_id
        self._retry = retry or RetryPolicy()
        self._on_retry = on_retry

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, url: str, **kw) -> dict:
        return request(self._client, "notion", method, url, self._retry, self._on_retry, **kw).json()

    def get_task(self, page_id: str) -> NotionTask:
        page = self._call("GET", f"/pages/{normalize_page_id(page_id)}")
        parent = page.get("parent", {})
        if parent.get("data_source_id", "").replace("-", "") != self.data_source_id.replace("-", ""):
            raise ConnectorResponseError("notion", "page is not in the Research Tasks data source")
        lines = list(self._walk(page["id"]))
        sections, other, preamble = split_sections(lines)
        props = page.get("properties", {})
        return NotionTask(
            page_id=page["id"],
            url=page.get("url", ""),
            title=_plain(props.get(PROP_TITLE)),
            status=_select(props.get(PROP_STATUS)),
            pending=bool((props.get(PROP_PENDING) or {}).get("checkbox", False)),
            linear_issue_id=_plain(props.get(PROP_LINEAR_ID)) or None,
            category=_select(props.get(PROP_CATEGORY)),
            last_edited_time=page.get("last_edited_time", ""),
            sections=sections,
            other_sections=other,
            preamble=preamble,
            fetched_at=utcnow(),
            run_requested=bool((props.get(PROP_RUN_REQUESTED) or {}).get("checkbox", False)),
            execution_status=_select(props.get(PROP_EXECUTION_STATUS)),
            last_run_url=(props.get(PROP_LAST_RUN) or {}).get("url"),
            execution_error=_plain(props.get(PROP_EXECUTION_ERROR)) or None,
        )

    def find_page_ids_by_linear_id(self, identifier: str) -> list[str]:
        body = {"filter": {"property": PROP_LINEAR_ID, "rich_text": {"equals": identifier}}, "page_size": 10}
        res = self._call("POST", f"/data_sources/{self.data_source_id}/query", json=body)
        return [r["id"] for r in res.get("results", [])]

    def requested_task_ids(self) -> list[str]:
        """Return pages with the Boolean request signal set; Button itself is not an API event."""
        body = {"filter": {"property": PROP_RUN_REQUESTED, "checkbox": {"equals": True}}, "page_size": 100}
        res = self._call("POST", f"/data_sources/{self.data_source_id}/query", json=body)
        return [r["id"] for r in res.get("results", [])]

    def _walk(self, block_id: str, depth: int = 0, budget: list[int] | None = None):
        budget = budget if budget is not None else [MAX_BLOCKS]
        cursor = None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            res = self._call("GET", f"/blocks/{block_id}/children", params=params)
            for b in res.get("results", []):
                budget[0] -= 1
                if budget[0] < 0:
                    raise ConnectorResponseError("notion", f"page exceeds {MAX_BLOCKS} blocks")
                yield from _block_lines(b, depth)
                if b.get("has_children") and depth < MAX_DEPTH and b["type"] not in ("child_page", "child_database"):
                    yield from self._walk(b["id"], depth + 1, budget)
            if not res.get("has_more"):
                return
            cursor = res.get("next_cursor")


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    t = prop.get("type")
    return "".join(x.get("plain_text", "") for x in prop.get(t, []) if isinstance(x, dict)).strip()


def _select(prop: dict | None) -> str | None:
    if not prop:
        return None
    value = prop.get(prop.get("type"))
    return value.get("name") if isinstance(value, dict) else None


def _block_lines(b: dict, depth: int):
    t = b.get("type", "")
    data = b.get(t, {}) or {}
    text = "".join(x.get("plain_text", "") for x in data.get("rich_text", []))
    indent = "  " * depth
    if t in ("heading_1", "heading_2", "heading_3"):
        yield int(t[-1]), text
    elif t == "bulleted_list_item":
        yield 0, f"{indent}- {text}"
    elif t == "numbered_list_item":
        yield 0, f"{indent}1. {text}"
    elif t == "to_do":
        yield 0, f"{indent}- [{'x' if data.get('checked') else ' '}] {text}"
    elif t == "code":
        yield 0, f"```{data.get('language', '')}\n{text}\n```"
    elif t == "equation":
        yield 0, f"$${data.get('expression', '')}$$"
    elif t == "table_row":
        cells = [" ".join(x.get("plain_text", "") for x in cell) for cell in data.get("cells", [])]
        yield 0, "| " + " | ".join(cells) + " |"
    elif t in ("quote", "callout"):
        yield 0, f"{indent}> {text}"
    elif text:
        yield 0, f"{indent}{text}"
