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

なし（引き継ぎ基盤の整備のみ完了。Phase 1 は未着手）

## Current Task

開発基盤・引き継ぎ基盤の構築、パッケージ名の統一、`.gitignore` 追加、初回 commit

## Status

完了（Phase 1 の仕様提供待ち）

## Last Updated

2026-10-09 / Claude Code

## Git Branch / Commit

- Branch: `main`
- Commit: 初回 commit（ハッシュは `git log` を参照）。この記録は初回 commit に含まれる。

## Completed Work

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

なし

## Remaining Work

- Phase 1 の仕様書 `docs/phase_specs/phase1.md` の提供（ユーザー）

## Changed Files

- `AGENTS.md`, `CLAUDE.md`, `docs/README.md`, `docs/development_handoff.md`（新規）
- `.gitignore`（新規）
- `src/researchautopilot/__init__.py` → `src/research_autopilot/__init__.py`（移動、出力文字列のみ変更）
- `pyproject.toml`, `uv.lock`（プロジェクト名・script の変更）

## Tests and Verification

- pytest は未実行（`tests/` にテストが存在しない）。
- 実行して確認したこと：
  - `uv sync`：成功（`researchautopilot` がアンインストールされ `research-autopilot` がインストールされた）。
  - `uv run research-autopilot`：`Hello from research-autopilot!` を出力。
  - `uv run python -c "import research_autopilot, research_autopilot.flows"`：成功。
  - `.venv/` が無視されていることを `git check-ignore` で確認。

## Known Issues / Blockers

1. **Notion / Linear へのアクセス不可**：エージェントは外部仕様を取得できない。仕様はローカル（`docs/phase_specs/`）で提供される必要がある。
2. **Phase 1 の仕様書が未提供**。
3. `data/` の扱い（大きな計算結果を Git 管理外にするか等）は未決定。現状 `.gitignore` では除外していない。

## Exact Next Actions

1. ユーザー：Phase 1 の要件定義を提供し、`docs/phase_specs/phase1.md` として格納する。
2. エージェント：`AGENTS.md` §4 の手順（git 状態確認 → この記録 → `phase1.md` を読む）に従い、作業ブランチ（例：`phase1/<topic>`）を切ってから Phase 1 に着手する。仕様書がない間は実装を始めない。
