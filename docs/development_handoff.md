# Development Handoff

Codex / Claude Code が作業を再開するときに最初に読む引き継ぎ記録です。
**進捗の記録であり、仕様の正本ではありません**（仕様は `docs/phase_specs/` とユーザーの指示）。

更新ルール（詳細は `AGENTS.md` §8）：

- 重要な実装単位の完了時、セッション終了・中断前、ブロッカー発生時に必ず更新する。
- 事実のみを書き、未確認のことは「未確認」と書く。
- テスト結果は、実際に実行したものだけを、実行コマンドと結果つきで書く。
- 未解決の Known Issues は解決するまで消さない。

---

## Current Phase

Phase 1（技術調査・設計レビュ完了、機能実装は未着手）

## Current Task

Phase 1 の Prefect 3 / Docker Compose / PostgreSQL / Tailscale 技術調査と実装設計

## Status

設計文書完了。機能実装・Docker 起動・Tailscale 設定は行っていない。

## Last Updated

2026-10-10 / Codex

## Git Branch / Commit

- Branch: `phase1/technical-design`
- Base commit: `0d3a438 Initial commit: project scaffold and agent handoff infrastructure`
- Phase 1 設計文書の commit: この記録を含む設計 commit（hash は `git log` を参照）

## Completed Work

### Phase 1 技術調査・設計（2026-10-10 / Codex）

- ユーザーが提供した Claude Code 向け指示の Section 3〜8 から Phase 1 要件（FR-01〜05、NFR、AC-01〜10）を読み取った。
- ユーザー指示により `docs/phase_specs/phase1.md` は追加していない。実装に必要な要件は `docs/phase1_technical_design.md` 内の「Phase 1 実装契約」に収録した。
- Prefect、Tailscale、Docker、PostgreSQL の現行公式ドキュメントを調査し、公式 URL とともに `docs/phase1_technical_design.md` に記録した。
- 確認済み事実、設計上の推奨、未確認 / 制約を区別した。
- Phase 1 の最小構成を `postgres` + 単一 process `prefect-server` + Process `prefect-worker` の3サービスとし、Redis 追加条件を明示した。
- container 内 Worker API URL、browser 用 UI API URL、MacBook 用 API URL、host-local URL を分離した。
- Tailscale Serve + localhost port bind + grants + Prefect Basic Auth を推奨し、Funnel と public port publish を使わない設計とした。
- retry と exactly-once の違い、PostgreSQL metadata と result storage の違い、container restart と in-flight Run recovery の違いを明記した。
- Compose 実装契約、障害モード、冪等性、通知分離、backup / restore、テストマトリクス、要件トレーサビリティを設計した。

### 開発基盤（2026-10-09 / Claude Code）

- リポジトリの現状を確認。
- `AGENTS.md`（共通開発ルール）、`CLAUDE.md`（`@AGENTS.md` のみ）、`docs/README.md`、`docs/development_handoff.md` を作成。
- パッケージ名を `research_autopilot` に統一（ユーザー指示）：
  - `src/researchautopilot/__init__.py` を `src/research_autopilot/__init__.py` に移動し、`src/researchautopilot/` を削除。
  - `pyproject.toml`：プロジェクト名 `researchautopilot` → `research-autopilot`、script を `research-autopilot = "research_autopilot:main"` に変更（uv_build が `research_autopilot` モジュールを自動解決するため）。
  - `uv sync` により `uv.lock` 内のプロジェクト名も更新。依存パッケージの追加・削除はなし。
- `.gitignore` を追加（Python キャッシュ、build 生成物、`.venv/`、テスト/lint キャッシュ、`.env` / `.env.*`（`.env.example` は除く）、`.prefect/`、ログ、OS/エディタ設定）。
- 初回 commit（ユーザー許可済み）。

### リポジトリ情報（2026-10-09 時点）

- `pyproject.toml`：name `research-autopilot`、Python `>=3.12`、build backend `uv_build`
  - dependencies：`prefect>=3.8.8`, `pydantic>=2.14.0`, `typer>=0.27.3`
  - dev：`pytest>=9.1.1`
  - script：`research-autopilot = "research_autopilot:main"`
- `.python-version`：3.12
- `src/research_autopilot/__init__.py`：雛形の `main()`（Hello 出力のみ）
- `src/research_autopilot/flows/__init__.py`：空
- `tests/`, `data/`, `infra/`：空ディレクトリ（Git は空ディレクトリを追跡しないため commit には含まれない）
- `README.md`：空
- OpenAI Agents SDK、Docker Compose、PostgreSQL 関連のファイル・依存：なし

## Work In Progress

なし。Phase 1 機能実装はまだ開始していない。

## Remaining Work

- `docs/phase1_technical_design.md` の実装順序に従い Phase 1 を実装する。
- ローカルで pytest、Flow、Compose build / health / restart / persistence、backup / restore rehearsal を検証する。
- 研究室サーバー / Tailscale / スマートフォンの実環境で AC-03〜07 の該当範囲を検証する。
- AC-01〜10 を Passed / Failed / Not Tested で報告する。

## Changed Files

- `docs/phase1_technical_design.md`（新規）
- `docs/README.md`（設計文書を一覧に追加）
- `docs/development_handoff.md`（本引き継更新）
- Python / Docker / Prefect 機能ファイルの変更はなし。

## Tests and Verification

- 2026-10-10 作業開始時: `uv run pytest` を実行。pytest 自体は起動したが `collected 0 items`、exit code 5。既存 test なしの基準状態であり、成功とは報告しない。
- `git diff --check`: 成功（whitespace error なし）。
- 公式 URL: Prefect / Tailscale / Docker / PostgreSQL の各ページを Web 調査時に閲覧し、本文の根拠と照合。自動 link checker は未実行。
- Docker / Compose / Tailscale / Prefect Flow: ユーザー指示により実裌・実行していない。
- 2026-10-09 に実行済みと記録されていること:
  - `uv sync`：成功（`researchautopilot` がアンインストールされ `research-autopilot` がインストールされた）。
  - `uv run research-autopilot`：`Hello from research-autopilot!` を出力。
  - `uv run python -c "import research_autopilot, research_autopilot.flows"`：成功。
  - `.venv/` が無視されていることを `git check-ignore` で確認。

## Known Issues / Blockers

1. **Notion / Linear へのアクセス不可**：エージェントは外部仕様を取得できない。仕様はローカル（`docs/phase_specs/`）で提供される必要がある。
2. **Phase 1 の SSOT ファイルがない**: `docs/phase_specs/phase1.md` は存在しない。ただし、2026-10-10 のユーザー指示で添付要件の読み取りと仕様ファイル非追加が明示された。実装に必要な契約は `docs/phase1_technical_design.md` に収録済み。
3. **実環境情報が未確認**: 研究室 server OS / CPU、Docker / Compose version、tailnet FQDN / grants 対象、backup 保存先、notification endpoint は未確認。placeholder でローカル実装可能だが、実機 AC は Not Tested とする。
4. **in-flight Run の自動復旧は保証できない**: container restart と Run resume は別問題。heartbeat Automation で `Crashed` を検出し、外部副作用を確認後に手動 retry する設計。
5. `data/` の扱い（大きな計算結果を Git 管理外にするか等）は未決定。Phase 1 で研究成果物 storage を推測して追加しない。

## Exact Next Actions

1. `git status` / branch / log とこのファイルを確認する。Phase 1 実装の要件・構成・値・制約・テストは `docs/phase1_technical_design.md` を単一の実装入力として読む。
2. `docs/phase1_technical_design.md` §15 の順で、まず exact version / environment schema と3-service Compose（PostgreSQL、単一 process Prefect Server、Process Worker）を実装する。Redis は現在の最小構成に追加しない。
3. 正常 / 異常 / retry / timeout / atomic idempotent side-effect Flow、HTTP notification adapter、pytest を実装する。
4. `uv run pytest`、`docker compose config`、build、health、deployment、restart / persistence、backup / restore rehearsal を実行し、実行した結果だけを記録する。
5. Tailscale と研究室 server への変更はユーザーの明示的な本番作業指示の範囲で行う。実環境がなければ AC-03〜07 の該当項目を Not Tested とする。
6. 意味のある実装単位ごとに test と commit を行い、各単位でこの引き継記録を更新する。
