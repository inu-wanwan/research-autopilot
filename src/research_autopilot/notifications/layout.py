"""Short, Japanese-first layout of a notification for Discord (user request 2026-10-14).

A message reads top-down as ``[見出し] 内容`` blocks: the task, what happened / what is asked, the details that
matter for the decision, the numbered steps (UX policy 1.5) and the Notion / Linear links.  Long lists are cut with
"ほか N 件" (the full text lives in Notion / Linear) and identifiers (request ids, run ids, commit SHAs) are shortened
and moved to a small reference line at the very end.

Producers keep writing ``NotificationEvent.details`` as ``"<prefix>: <text>"`` lines; this module only sorts those
lines into headings, so events already queued in the outbox render the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from research_autopilot.notifications.events import EventKind, NotificationEvent, numbered

DESCRIPTION_LIMIT = 4096  # Discord embed description
TITLE_LIMIT = 256
FOOTER_LIMIT = 2048
TOTAL_LIMIT = 6000  # title + description + fields + footer of one embed
_BODY_BUDGET = 3900  # leave room under DESCRIPTION_LIMIT for the truncation note

_MORE = "（詳細は Notion / Linear）"

# approval kind (state_name "<kind>:<status>") -> short Japanese name for the title
APPROVAL_KIND_JA = {
    "onboarding": "Autopilot への委任", "budget": "予算の増額", "completion_review": "完了の確認",
    "proposal": "追加実験", "dag": "子タスクの依存関係", "research_scope": "研究計画", "plan": "計画",
    "external_code": "外部コードの実行", "publish": "PR の作成", "external_license": "外部コードのライセンス",
}

_KIND_JA = {
    EventKind.COMPLETED: "完了", EventKind.FAILED: "失敗", EventKind.APPROVAL_REQUIRED: "承認依頼",
    EventKind.EXPERIMENT_COMPLETED: "実験完了", EventKind.ANALYSIS_COMPLETED: "評価",
    EventKind.TASK_COMPLETED: "完了", EventKind.UNRECOVERABLE_ERROR: "エラー", EventKind.BUDGET_EXCEEDED: "予算超過",
    EventKind.WORKER_FAILURE: "ワーカー障害", EventKind.TASK_STARTED: "開始", EventKind.PHASE_TRANSITION: "進捗",
    EventKind.AUTOMATIC_RETRY: "自動再試行", EventKind.REPAIR_SUCCEEDED: "修復成功",
    EventKind.PROPOSAL_GENERATED: "提案",
}

# link prefix -> label shown in the [リンク] line
_LINK_LABELS = {
    "Notion plan": "Notion 計画", "Notion Notes": "Notion 結果", "Notes": "Notion 結果", "Notion": "Notion",
    "Task": "Notion タスク", "Linear": "Linear", "Pull Request": "Pull Request", "Prefect": "Prefect",
    "Approval form": "承認フォーム",
}

# detail prefix -> section (checked in order; the prefix is removed from the shown text)
_SECTIONS: list[tuple[tuple[str, ...], str]] = [
    (("置き換え:", "失効:", "無効:"), "closed"),
    (("依頼:",), "ask"),
    (("対象:",), "subject"),
    (("変更:",), "changes"),
    (("理由:",), "reason"),
    (("予算:", "費用:", "Budget used:", "Budget:"), "budget"),
    (("期限:",), "deadline"),
    (("結果:", "Result:", "Outcome:"), "result"),
]

# detail prefix -> label in the reference line (the value is an identifier, shown shortened)
_REF_PREFIXES = {
    "Experiment run:": "実験", "Phase 3 run:": "実装", "Root task:": "ルート", "Chain depth:": "連鎖",
    "Commits:": "commit", "Attempt:": "試行", "Stage:": "段階",
}

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b[0-9a-f]{16,}\b")
_URL = re.compile(r"https?://\S+")
_SUPERSEDES = re.compile(r"^版 v(\d+): 以前の依頼 (\S+) を置き換えます$")


def short_id(value: str, n: int = 8) -> str:
    return value[:n]


def _shorten_ids(text: str) -> str:
    """UUIDs and long hex (hashes, SHAs) outside URLs -> their first 8 / 12 characters."""
    parts = []
    last = 0
    for m in _URL.finditer(text):
        parts.append(_shorten_plain(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_shorten_plain(text[last:]))
    return "".join(parts)


def _shorten_plain(text: str) -> str:
    text = _UUID.sub(lambda m: m.group(0)[:8], text)
    return _HEX.sub(lambda m: m.group(0)[:12], text)


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _limit_list(items: list[str], max_items: int, item_chars: int) -> list[str]:
    shown = [clip(x, item_chars) for x in items[:max_items]]
    if len(items) > max_items:
        shown.append(f"ほか {len(items) - max_items} 件{_MORE}")
    return shown


@dataclass
class Layout:
    title: str
    description: str
    footer: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)


def _route_name(event: NotificationEvent) -> str:
    from research_autopilot.notifications.discord import route_event
    return route_event(event).value


def _split_link(line: str) -> tuple[str, str] | None:
    """``"Notion: https://..."`` -> ("Notion", url); None for non-link lines."""
    head, sep, rest = line.partition(":")
    if not sep or head not in _LINK_LABELS:
        return None
    url = rest.strip()
    if url.startswith("http"):
        return _LINK_LABELS[head], url.split()[0]
    return "", ""  # a link line without a URL (page not created yet): nothing to show


def _classify(details: list[str]) -> tuple[dict[str, list[str]], list[tuple[str, str]], list[str]]:
    sections: dict[str, list[str]] = {}
    links: list[tuple[str, str]] = []
    refs: list[str] = []
    for raw in details:
        line = (raw or "").strip()
        if not line:
            continue
        link = _split_link(line)
        if link is not None:
            if link[1] and link not in links:
                links.append(link)
            continue
        ref = next((p for p in _REF_PREFIXES if line.startswith(p)), None)
        if ref is not None:
            refs.append(f"{_REF_PREFIXES[ref]} {_shorten_ids(line[len(ref):].strip())}")
            continue
        sup = _SUPERSEDES.match(line)
        if sup:
            sections.setdefault("version", []).append(f"v{sup.group(1)}（以前の依頼を置き換え）")
            refs.append(f"旧依頼 {sup.group(2)[:8]}")
            continue
        name = next((s for keys, s in _SECTIONS if line.startswith(keys)), None)
        if name is None:
            sections.setdefault("body", []).append(line)
            continue
        text = line if name == "closed" else line.split(":", 1)[1].strip()  # keep "失効: " / "置き換え: "
        sections.setdefault(name, []).append(text)
    return sections, links, refs


def _bullets(items: list[str]) -> str:
    return items[0] if len(items) == 1 else "\n".join(f"・{x}" for x in items)


def _closed_text(lines: list[str]) -> str:
    # "置き換え: ...。新しい依頼へ: https://discord.com/..." -> markdown link to the replacement
    out = []
    for line in lines:
        out.append(re.sub(r"新しい依頼へ: (https?://\S+)", r"[新しい依頼へ](\1)", line))
    return "\n".join(out)


def render(event: NotificationEvent) -> Layout:
    route = _route_name(event)
    sec, links, refs = _classify(event.details)
    task = event.flow_run_name or event.flow_name
    kind_name = None
    if event.kind is EventKind.APPROVAL_REQUIRED and event.state_name:
        kind_name = APPROVAL_KIND_JA.get(event.state_name.split(":", 1)[0])
    label = _KIND_JA.get(event.kind, event.kind.value)
    if event.needs_human and event.kind is not EventKind.APPROVAL_REQUIRED:
        label = "予算超過" if event.kind is EventKind.BUDGET_EXCEEDED else "対応が必要"
    title = f"{label}：{kind_name}｜{task}" if kind_name else f"{label}｜{task}"

    errors = route == "errors"
    reason_chars = 900 if errors else 500
    body_heading = {"approvals": "状況", "results": "結果", "errors": "内容", "logs": "内容"}[route]

    blocks: list[tuple[str, str]] = []
    if sec.get("closed"):
        blocks.append(("この依頼は無効です", _closed_text(sec["closed"])))
    subject = sec.get("subject", [])
    blocks.append(("タスク", clip(" ".join([task, *subject]) if subject else task, 200)))
    if sec.get("ask"):
        ask = _bullets([clip(x, 200) for x in sec["ask"]])
        if sec.get("version"):
            ask += f"\n{sec['version'][0]}"
        blocks.append(("確認してほしいこと", ask))
    body = sec.get("body", [])
    result = sec.get("result", [])
    if route == "results":
        body, result = result + body, []
    if body:
        blocks.append((body_heading, _bullets(_limit_list([_shorten_ids(x) for x in body], 4, reason_chars))))
    if result:
        blocks.append(("結果", _bullets(_limit_list([_shorten_ids(x) for x in result], 3, 300))))
    if event.error_type:
        blocks.append(("エラー", event.error_type[:200]))
    if sec.get("changes"):
        blocks.append(("変更点", _bullets(_limit_list(sec["changes"], 5, 200))))
    if sec.get("reason"):
        blocks.append(("理由", _bullets(_limit_list([_shorten_ids(x) for x in sec["reason"]], 3, reason_chars))))
    if sec.get("budget"):
        blocks.append(("予算・費用", _bullets(_limit_list(sec["budget"], 3, 200))))
    if sec.get("deadline"):
        blocks.append(("期限", clip(sec["deadline"][0], 120)))
    if event.steps:
        steps = _limit_list(event.steps, 8, 220)
        blocks.append(("次にやること", "\n".join(numbered(steps))))
    if links:
        blocks.append(("リンク", " / ".join(f"[{name}]({url})" for name, url in links)))

    if event.approval_request_id:
        refs.insert(0, f"req {short_id(event.approval_request_id)}")
    if event.state_name:
        refs.append(f"状態 {event.state_name}")
    if event.flow_run_id:
        refs.append(f"run {short_id(event.flow_run_id)}")
    refs.append(f"flow {event.flow_name}")
    footer = "参照: " + " · ".join(clip(r, 60) for r in refs)

    description = _join(blocks)
    return Layout(title=clip(title, TITLE_LIMIT), description=description, footer=clip(footer, 600), sections=blocks)


def _join(blocks: list[tuple[str, str]]) -> str:
    """``**[見出し]** 内容``; a multi-line value starts on its own line. Cut to the Discord limit, keeping the
    steps and links (they say how to continue) when the body is long."""
    def fmt(name: str, value: str) -> str:
        return f"**[{name}]**\n{value}" if "\n" in value else f"**[{name}]** {value}"

    keep_tail = {"次にやること", "リンク"}
    tail = "\n".join(fmt(n, v) for n, v in blocks if n in keep_tail)
    head_parts = [fmt(n, v) for n, v in blocks if n not in keep_tail]
    budget = _BODY_BUDGET - len(tail)
    head = "\n".join(head_parts)
    if len(head) > budget:
        head = head[: max(0, budget - len(_MORE) - 2)].rstrip() + "…" + _MORE
    text = "\n".join(x for x in (head, tail) if x)
    return text[:DESCRIPTION_LIMIT]


__all__ = ["APPROVAL_KIND_JA", "Layout", "clip", "render"]
