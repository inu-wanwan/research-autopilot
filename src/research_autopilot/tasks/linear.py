"""Linear connector (GraphQL).

Reads issues of the Research project. The only write is ``set_state``,
which the orchestrator calls only in apply mode, with the target state
restricted to type ``started`` (never ``completed``).
"""

from collections.abc import Callable

import httpx

from research_autopilot.models import IssueRef, LinearIssue, utcnow
from research_autopilot.tasks.errors import ConnectorResponseError, NotFoundError
from research_autopilot.tasks.http import RetryPolicy, request
from research_autopilot.tasks.notion import notion_ids_in_text

API = "https://api.linear.app/graphql"

_ISSUE_FIELDS = """
  id identifier title url description priority createdAt updatedAt
  state { id name type }
  team { id }
  project { id }
  labels(first: 50) { nodes { name } }
  attachments(first: 20) { nodes { url } }
  inverseRelations(first: 50) { nodes { type issue { identifier title state { name type } } } }
"""


class LinearClient:
    def __init__(self, api_key: str, project_id: str, *, transport: httpx.BaseTransport | None = None,
                 retry: RetryPolicy | None = None, on_retry: Callable[[str], None] | None = None,
                 timeout: float = 30.0) -> None:
        self._client = httpx.Client(timeout=timeout, transport=transport,
                                    headers={"Authorization": api_key, "Content-Type": "application/json"})
        self.project_id = project_id
        self._retry = retry or RetryPolicy()
        self._on_retry = on_retry

    def close(self) -> None:
        self._client.close()

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        resp = request(self._client, "linear", "POST", API, self._retry, self._on_retry,
                       json={"query": query, "variables": variables or {}})
        body = resp.json()
        if body.get("errors"):
            ext = body["errors"][0].get("extensions", {})
            code = ext.get("code") or ext.get("type") or "error"
            if "not found" in str(body["errors"][0].get("message", "")).lower() or code == "ENTITY_NOT_FOUND":
                raise NotFoundError("linear", code)
            raise ConnectorResponseError("linear", f"GraphQL {code}")
        return body["data"]

    def get_issue(self, identifier: str) -> LinearIssue:
        data = self._gql(f"query($id: String!) {{ issue(id: $id) {{ {_ISSUE_FIELDS} }} }}", {"id": identifier})
        if not data.get("issue"):
            raise NotFoundError("linear", identifier)
        return _to_issue(data["issue"])

    def candidate_identifiers(self, label: str, state_type: str) -> list[str]:
        """Issues of the project with ``label`` in ``state_type``, ordered by
        priority (urgent first, "no priority" last) then creation time."""
        query = """
        query($project: ID!, $label: String!, $stateType: String!, $after: String) {
          issues(first: 100, after: $after, filter: {
            project: { id: { eq: $project } },
            labels: { some: { name: { eq: $label } } },
            state: { type: { eq: $stateType } }
          }) { nodes { identifier priority createdAt } pageInfo { hasNextPage endCursor } }
        }"""
        nodes, after = [], None
        while True:
            data = self._gql(query, {"project": self.project_id, "label": label, "stateType": state_type, "after": after})
            nodes += data["issues"]["nodes"]
            if not data["issues"]["pageInfo"]["hasNextPage"]:
                break
            after = data["issues"]["pageInfo"]["endCursor"]
        nodes.sort(key=lambda n: (n["priority"] or 5, n["createdAt"], n["identifier"]))
        return [n["identifier"] for n in nodes]

    def team_state_id(self, team_id: str, name: str) -> tuple[str, str]:
        data = self._gql("query($id: String!) { team(id: $id) { states { nodes { id name type } } } }", {"id": team_id})
        for s in data["team"]["states"]["nodes"]:
            if s["name"] == name:
                return s["id"], s["type"]
        raise ConnectorResponseError("linear", f"workflow state {name!r} not found in team")

    def set_state(self, issue_id: str, state_id: str) -> str:
        data = self._gql(
            "mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: { stateId: $s }) { success issue { state { name } } } }",
            {"id": issue_id, "s": state_id},
        )
        if not data["issueUpdate"]["success"]:
            raise ConnectorResponseError("linear", "issueUpdate returned success=false")
        return data["issueUpdate"]["issue"]["state"]["name"]


def _to_issue(n: dict) -> LinearIssue:
    texts = [n.get("description") or ""] + [a["url"] for a in n["attachments"]["nodes"]]
    notion_ids: list[str] = []
    for t in texts:
        for pid in notion_ids_in_text(t):
            if pid not in notion_ids:
                notion_ids.append(pid)
    blocked_by = [
        IssueRef(identifier=r["issue"]["identifier"], title=r["issue"]["title"],
                 state_name=r["issue"]["state"]["name"], state_type=r["issue"]["state"]["type"])
        for r in n["inverseRelations"]["nodes"] if r["type"] == "blocks"
    ]
    return LinearIssue(
        id=n["id"], identifier=n["identifier"], title=n["title"], url=n["url"],
        description=n.get("description") or "", priority=n.get("priority") or 0,
        state_id=n["state"]["id"], state_name=n["state"]["name"], state_type=n["state"]["type"],
        team_id=n["team"]["id"], labels=[x["name"] for x in n["labels"]["nodes"]],
        project_id=(n.get("project") or {}).get("id"), blocked_by=blocked_by,
        notion_page_ids=notion_ids, created_at=n["createdAt"], updated_at=n["updatedAt"],
        fetched_at=utcnow(),
    )


class LinearWriter(LinearClient):
    """Phase 5 write operations (comments, issue creation, history). Status changes are
    restricted by the caller (autopilot.linear_ops) to non-approval states."""

    def team_states(self, team_id: str) -> dict[str, tuple[str, str]]:
        data = self._gql("query($id: String!) { team(id: $id) { states { nodes { id name type } } } }", {"id": team_id})
        return {s["name"]: (s["id"], s["type"]) for s in data["team"]["states"]["nodes"]}

    def label_ids(self, names: list[str]) -> list[str]:
        data = self._gql("{ issueLabels(first: 250) { nodes { id name } } }")
        by_name = {n["name"]: n["id"] for n in data["issueLabels"]["nodes"]}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise ConnectorResponseError("linear", f"labels not found: {missing}")
        return [by_name[n] for n in names]

    def create_comment(self, issue_id: str, body: str) -> str:
        data = self._gql("mutation($i: String!, $b: String!) { commentCreate(input: {issueId: $i, body: $b}) { success comment { id } } }",
                         {"i": issue_id, "b": body})
        if not data["commentCreate"]["success"]:
            raise ConnectorResponseError("linear", "commentCreate returned success=false")
        return data["commentCreate"]["comment"]["id"]

    def viewer_id(self) -> str:
        data = self._gql("{ viewer { id } }")
        return data["viewer"]["id"]

    def create_issue(self, team_id: str, title: str, description: str, state_id: str, label_ids: list[str]) -> tuple[str, str, str]:
        assignee_id = self.viewer_id()
        data = self._gql(
            "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier url } } }",
            {"in": {"teamId": team_id, "projectId": self.project_id, "title": title, "description": description,
                    "stateId": state_id, "labelIds": label_ids, "assigneeId": assignee_id}})
        if not data["issueCreate"]["success"]:
            raise ConnectorResponseError("linear", "issueCreate returned success=false")
        i = data["issueCreate"]["issue"]
        return i["id"], i["identifier"], i["url"]

    def update_description(self, issue_id: str, description: str) -> None:
        self._gql("mutation($id: String!, $d: String!) { issueUpdate(id: $id, input: {description: $d}) { success } }",
                  {"id": issue_id, "d": description})

    def find_by_marker(self, marker: str) -> list[dict]:
        data = self._gql(
            "query($p: ID!, $m: String!) { issues(first: 10, filter: { project: { id: { eq: $p } }, title: { contains: $m } }) "
            "{ nodes { id identifier url title } } }", {"p": self.project_id, "m": marker})
        return data["issues"]["nodes"]

    def history(self, identifier: str) -> list[dict]:
        """State transitions with actor information (actor = user; botActor = integration/app)."""
        data = self._gql(
            "query($id: String!) { issue(id: $id) { history(first: 50) { nodes { id createdAt actor { id name } "
            "botActor { name type } fromState { name } toState { name } } } } }", {"id": identifier})
        return [h for h in data["issue"]["history"]["nodes"] if h.get("toState")]
