import json

import httpx
import pytest

from research_autopilot.tasks.errors import (
    ConnectorAuthError,
    ConnectorResponseError,
    NotFoundError,
    TransientConnectorError,
)
from research_autopilot.tasks.http import RetryPolicy
from research_autopilot.tasks.linear import LinearClient
from research_autopilot.tasks.notion import NotionClient, normalize_page_id, notion_ids_in_text

DS = "3b469893-cf8f-802d-9f86-000bb4bbb21a"
PAGE = "3f469893-cf8f-817f-9f7e-f768fd99c9d3"
NO_SLEEP = RetryPolicy(attempts=3, base_delay=0, sleep=lambda s: None)


def rt(text):
    return [{"plain_text": text}]


def heading(level, text, has_children=False, bid="h"):
    return {"id": bid, "type": f"heading_{level}", f"heading_{level}": {"rich_text": rt(text)}, "has_children": has_children}


def para(text, bid="p"):
    return {"id": bid, "type": "paragraph", "paragraph": {"rich_text": rt(text)}, "has_children": False}


def page_json(linear_id="INU-265", parent_ds=DS, pending=False):
    return {
        "id": PAGE, "url": f"https://www.notion.so/{PAGE.replace('-', '')}", "last_edited_time": "2026-10-10T00:00:00.000Z",
        "parent": {"type": "data_source_id", "data_source_id": parent_ds},
        "properties": {
            "Name": {"type": "title", "title": rt("[AUTO] Phase 2")},
            "Status": {"type": "status", "status": {"name": "In progress"}},
            "Pending": {"type": "checkbox", "checkbox": pending},
            "Linear Issue ID": {"type": "rich_text", "rich_text": rt(linear_id)},
            "分類": {"type": "select", "select": {"name": "実装"}},
        },
    }


def notion_handler(blocks_pages, page=None, status_seq=None):
    """blocks_pages: list of block lists returned page by page for the root."""
    calls = {"n": 0, "status": list(status_seq or [])}

    def handler(request: httpx.Request):
        if calls["status"]:
            code = calls["status"].pop(0)
            return httpx.Response(code, json={"code": "rate_limited" if code == 429 else "err"})
        path = request.url.path
        assert request.headers["Notion-Version"] == "2025-09-03"
        if path == f"/v1/pages/{PAGE}":
            return httpx.Response(200, json=page or page_json())
        if path == f"/v1/blocks/{PAGE}/children":
            i = calls["n"]
            calls["n"] += 1
            return httpx.Response(200, json={"results": blocks_pages[i], "has_more": i + 1 < len(blocks_pages),
                                             "next_cursor": f"c{i + 1}" if i + 1 < len(blocks_pages) else None})
        if path == "/v1/blocks/child/children":
            return httpx.Response(200, json={"results": [para("nested detail")], "has_more": False})
        if path == f"/v1/data_sources/{DS}/query":
            body = json.loads(request.content)
            assert body["filter"] == {"property": "Linear Issue ID", "rich_text": {"equals": "INU-265"}}
            return httpx.Response(200, json={"results": [{"id": PAGE}], "has_more": False})
        return httpx.Response(404, json={"code": "object_not_found"})

    return handler


def notion(handler, **kw):
    return NotionClient("secret", DS, transport=httpx.MockTransport(handler), retry=NO_SLEEP, **kw)


def test_notion_get_task_maps_sections_across_pages():
    blocks = [
        [heading(2, "Purpose"), para("goal text"), heading(2, "Implementation"), para("do x")],
        [heading(3, "1. Details", has_children=False), para("more"),
         {"id": "child", "type": "toggle", "toggle": {"rich_text": rt("toggle")}, "has_children": True},
         heading(2, "完了条件"), para("- table exists"), heading(2, "Inputs"), para("data"), heading(2, "Notes"), para("n")],
    ]
    task = notion(notion_handler(blocks)).get_task(PAGE)
    assert task.title == "[AUTO] Phase 2" and task.linear_issue_id == "INU-265" and task.pending is False
    assert task.sections["purpose"] == "goal text"
    assert "do x" in task.sections["specification"] and "### 1. Details" in task.sections["specification"]
    assert "nested detail" in task.sections["specification"]
    assert task.sections["acceptance_criteria"] == "- table exists"
    assert task.sections["input_artifacts"] == "data" and task.other_sections == {"Notes": "n"}


def test_notion_rejects_page_outside_data_source():
    with pytest.raises(ConnectorResponseError):
        notion(notion_handler([[]], page=page_json(parent_ds="other"))).get_task(PAGE)


def test_notion_find_by_linear_id():
    assert notion(notion_handler([[]])).find_page_ids_by_linear_id("INU-265") == [PAGE]


def test_notion_retries_429_then_succeeds():
    retries = []
    task = notion(notion_handler([[heading(2, "Purpose")]], status_seq=[429, 503]), on_retry=retries.append).get_task(PAGE)
    assert task.page_id == PAGE and len(retries) == 2


def test_notion_gives_up_after_retries():
    with pytest.raises(TransientConnectorError):
        notion(notion_handler([[]], status_seq=[500, 500, 500])).get_task(PAGE)


@pytest.mark.parametrize("code,exc", [(401, ConnectorAuthError), (403, ConnectorAuthError),
                                      (404, NotFoundError), (400, ConnectorResponseError)])
def test_notion_non_retryable_errors(code, exc):
    with pytest.raises(exc) as e:
        notion(notion_handler([[]], status_seq=[code])).get_task(PAGE)
    assert "secret" not in str(e.value)


def test_notion_transport_error_is_transient():
    def handler(request):
        raise httpx.ConnectTimeout("timeout", request=request)
    with pytest.raises(TransientConnectorError):
        notion(handler).get_task(PAGE)


def test_page_id_parsing():
    assert normalize_page_id("https://app.notion.com/p/AUTO-Phase-1-3f469893cf8f81f2835fc84007f2f39c?source=copy_link") \
        == "3f469893-cf8f-81f2-835f-c84007f2f39c"
    assert normalize_page_id(PAGE.replace("-", "")) == PAGE
    with pytest.raises(ValueError):
        normalize_page_id("not-an-id")
    text = "詳細仕様SSOT: [https://app.notion.com/p/3f469893cf8f817f9f7ef768fd99c9d3](<https://app.notion.com/p/3f469893cf8f817f9f7ef768fd99c9d3>)"
    assert notion_ids_in_text(text) == [PAGE]


# ---------------------------------------------------------------- Linear

ISSUE = {
    "id": "uuid-265", "identifier": "INU-265", "title": "Phase 2", "url": "https://linear.app/x/INU-265",
    "description": "Notion SSOT: [https://app.notion.com/p/3f469893cf8f817f9f7ef768fd99c9d3](<https://app.notion.com/p/3f469893cf8f817f9f7ef768fd99c9d3>)",
    "priority": 2, "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-10T00:00:00Z",
    "state": {"id": "s-todo", "name": "Todo", "type": "unstarted"}, "team": {"id": "team-1"},
    "project": {"id": "proj"}, "labels": {"nodes": [{"name": "autopilot"}]},
    "attachments": {"nodes": [{"url": "https://app.notion.com/p/3f469893cf8f817f9f7ef768fd99c9d3"}]},
    "inverseRelations": {"nodes": [
        {"type": "blocks", "issue": {"identifier": "INU-264", "title": "P1", "state": {"name": "Done", "type": "completed"}}},
        {"type": "related", "issue": {"identifier": "INU-1", "title": "x", "state": {"name": "Todo", "type": "unstarted"}}},
    ]},
}


def linear(handler):
    return LinearClient("lin_api_secret", "proj", transport=httpx.MockTransport(handler), retry=NO_SLEEP)


def test_linear_get_issue_parses_links_and_blockers():
    def handler(request):
        assert request.headers["Authorization"] == "lin_api_secret"
        return httpx.Response(200, json={"data": {"issue": ISSUE}})
    issue = linear(handler).get_issue("INU-265")
    assert issue.state_type == "unstarted" and issue.labels == ["autopilot"] and issue.priority == 2
    assert issue.notion_page_ids == [PAGE]  # de-duplicated across description + attachment
    assert [b.identifier for b in issue.blocked_by] == ["INU-264"]  # "related" is not a dependency


def test_linear_not_found():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "Entity not found: Issue",
                                                     "extensions": {"code": "ENTITY_NOT_FOUND"}}]})
    with pytest.raises(NotFoundError):
        linear(handler).get_issue("INU-404")


def test_linear_graphql_error_is_response_error():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "Query too complex", "extensions": {"code": "INPUT_ERROR"}}]})
    with pytest.raises(ConnectorResponseError):
        linear(handler).get_issue("INU-265")


def test_linear_rate_limit_retried():
    seq = [429, 200]

    def handler(request):
        code = seq.pop(0)
        return httpx.Response(code, json={"data": {"issue": ISSUE}} if code == 200 else {})
    assert linear(handler).get_issue("INU-265").identifier == "INU-265"


def test_linear_candidates_sorted_and_paginated():
    pages = [
        {"nodes": [{"identifier": "INU-3", "priority": 0, "createdAt": "2026-01-01"},
                   {"identifier": "INU-2", "priority": 3, "createdAt": "2026-01-02"}],
         "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
        {"nodes": [{"identifier": "INU-1", "priority": 1, "createdAt": "2026-01-03"}],
         "pageInfo": {"hasNextPage": False, "endCursor": None}},
    ]

    def handler(request):
        v = json.loads(request.content)["variables"]
        assert v["label"] == "autopilot" and v["stateType"] == "unstarted" and v["project"] == "proj"
        return httpx.Response(200, json={"data": {"issues": pages.pop(0)}})
    assert linear(handler).candidate_identifiers("autopilot", "unstarted") == ["INU-1", "INU-2", "INU-3"]


def test_linear_set_state_success_and_failure():
    results = [{"success": True, "issue": {"state": {"name": "In Progress"}}}, {"success": False, "issue": None}]

    def handler(request):
        body = json.loads(request.content)
        assert "issueUpdate" in body["query"] and body["variables"] == {"id": "uuid-265", "s": "s-ip"}
        return httpx.Response(200, json={"data": {"issueUpdate": results.pop(0)}})
    client = linear(handler)
    assert client.set_state("uuid-265", "s-ip") == "In Progress"
    with pytest.raises(ConnectorResponseError):
        client.set_state("uuid-265", "s-ip")


def test_linear_team_state_lookup():
    def handler(request):
        return httpx.Response(200, json={"data": {"team": {"states": {"nodes": [
            {"id": "s1", "name": "Todo", "type": "unstarted"}, {"id": "s2", "name": "In Progress", "type": "started"}]}}}})
    client = linear(handler)
    assert client.team_state_id("team-1", "In Progress") == ("s2", "started")
    with pytest.raises(ConnectorResponseError):
        client.team_state_id("team-1", "Doing")
