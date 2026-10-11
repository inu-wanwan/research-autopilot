"""Notion Research Note for a literature report: citations, short quotes and open issues only (no PDF bodies)."""

from __future__ import annotations

from research_autopilot.literature.models import LiteratureReport
from research_autopilot.tasks.notion_writer import bullet, heading, paragraph, table, toggle

CATEGORY = "文献"
MAX_QUOTE = 280


def cite(src) -> str:
    who = (src.authors[0].split()[-1] + (" et al." if len(src.authors) > 1 else "")) if src.authors else ""
    ident = f"doi:{src.doi}" if src.doi and not src.doi.startswith("10.48550") else (
        f"arXiv:{src.arxiv_id}{src.version or ''}" if src.arxiv_id else (src.url or ""))
    if src.source_type.value == "reference_implementation":
        ident = f"{src.url} @ {(src.version or '')[:12]}"
    return f"{who} {src.year or ''} — {src.title} ({ident})".strip()


def blocks(report: LiteratureReport) -> list[dict]:
    srcs = {s.source_id: s for s in report.sources}
    out = [heading("問い"), paragraph(report.question),
           heading("要約"), paragraph(report.summary or "（統合できた主張がありません）"),
           heading("資料"),
           table(["ID", "種別", "資料", "取得", "取得日時"],
                 [[s.source_id, s.source_type.value, cite(s)[:300],
                   f"{s.retrieval.method} / {s.retrieval.status}",
                   s.retrieval.retrieved_at.strftime("%Y-%m-%d %H:%M") if s.retrieval.retrieved_at else "-"]
                  for s in report.sources] or [["-", "-", "-", "-", "-"]])]
    verified = report.verified_claims
    out.append(heading(f"確認済みの主張（{len(verified)}）"))
    for c in verified[:40]:
        q = c.quote if len(c.quote) <= MAX_QUOTE else c.quote[:MAX_QUOTE] + "…"
        out.append(bullet(f"[{c.kind.value}/{c.origin}] {c.statement} — {c.source_id} {c.locator}: “{q}”"))
    unverified = [c for c in report.claims if c.status != "verified"]
    if unverified:
        out.append(heading(f"未確認の主張（{len(unverified)}・原文で引用を確認できず）"))
        out += [bullet(f"[{c.kind.value}] {c.statement} — {c.source_id}") for c in unverified[:20]]
    if report.conflicts:
        out.append(heading("矛盾・不一致"))
        out += [bullet(f"{x.topic}: {x.description} ({', '.join(x.claim_ids)})") for x in report.conflicts]
    out.append(heading("未解決・未確認"))
    out += [bullet(u) for u in report.unresolved[:30]] or [paragraph("なし")]
    if report.failures:
        out.append(heading("取得・処理の失敗"))
        out += [bullet(f"{f.stage} {f.target}: {f.detail}" + (" （人の対応が必要）" if f.needs_human else ""))
                for f in report.failures[:30]]
    out.append(toggle("再現情報", [paragraph(f"report_id: {report.report_id}"),
                                    paragraph(f"topic_key: {report.topic_key}"),
                                    paragraph(f"models: {', '.join(report.models)} / cost ${report.total_cost_usd:.4f}"),
                                    *[paragraph(f"{s.source_id} sha256 {s.retrieval.sha256 or '-'}")
                                      for s in report.sources]]))
    return out


def write_note(writer, report: LiteratureReport) -> tuple[str, str]:
    title = f"[文献] {report.question[:80]}"
    return writer.create_note(title, blocks(report), category=CATEGORY)
