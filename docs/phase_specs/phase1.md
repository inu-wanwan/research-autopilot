# Phase 1 仕様 — 自律実行・監視基盤（Execution Infrastructure）

- 作成：2026-10-10 / Claude Code（ユーザー指示により作成）
- 出典：ユーザーが提示した「Task: Research Autopilot — Phase 1 実装」指示の §1, §3〜§8, §10
- 外部仕様の正本：Notion タスク https://app.notion.com/p/3f469893cf8f81f2835fc84007f2f39c
  - エージェントは Notion にアクセスできないため、本ファイルをローカルの実装入力とする。
  - 本ファイルは提示内容の整理であり、要件の追加・解釈による変更はしていない。技術的な設計判断は `docs/phase1_technical_design.md` を参照。
- 変更：ユーザーの指示・承認がある場合のみ（`AGENTS.md` §5）。

---

## 1. Objective

Research Autopilot は、研究者が Notion / Linear で定義・承認した研究タスクを、複数のエージェントが実装・実験・解析まで自律的に進めるシステムである。完成形では OpenAI Agents SDK による Orchestrator、Codex による Implementer、計算実験を管理する Experiment Runner、結果を検証する Analyst / Reviewer を統合する。

**Phase 1 の目的は、これらのエージェントが将来動作するための、常時稼働・ワークフロー管理・監視・障害復旧の基盤を構築することである。**

- 開発は MacBook、実際の稼働は常時インターネット接続されている研究室 Linux サーバーで行う。
- 大規模な計算実験は将来 TSUBAME に委譲する予定だが、Phase 1 では TSUBAME との連携は実装しない。
- 未記載の科学的仕様や研究タスク連携機能は推測して追加しない。

## 2. Architecture

| Component | Technology |
|---|---|
| Language | Python |
| Package manager | uv |
| Workflow Engine | Prefect 3 |
| Deployment | Docker Compose |
| Prefect Hosting | Self-hosted |
| Database | PostgreSQL |
| Remote Monitoring | Tailscale |
| Notification | ntfy 等の HTTP ベース通知 |
| Testing | pytest |
| Execution host | 研究室 Linux サーバー |

- **Prefect Cloud は使用しない。**
- 研究室サーバーに Prefect Server / UI、Worker、PostgreSQL を配置する。Redis は利用する Prefect 構成で必要な場合のみ追加する。
- Prefect の標準的な状態管理機能を優先し、独自の Workflow Engine や重複する状態管理 DB は実装しない。

```text
MacBook
  ├── Development
  ├── Git
  └── Flow deployment / triggering
          |
          v
Research Lab Linux Server
  ├── Docker Compose
  │     ├── Prefect Server / UI
  │     ├── Prefect Worker
  │     └── PostgreSQL
  │
  ├── Workflow execution
  └── Persistent logs / state
          |
          v
      Tailscale
          |
          v
Smartphone
  └── Prefect UI / Run status / Logs
```

- MacBook とスマートフォンから Prefect API / UI に安全にアクセスできるようにする。
- Prefect UI を認証なしで公開インターネットへ露出させない。

## 3. Functional Requirements

### FR-01: Workflow execution

- Prefect Flow / Task を定義し、研究室サーバーの Worker で実行できること。
- MacBook からフローを登録・起動できること。
- 手動起動とスケジュール起動をサポートすること。
- パラメータ、実行時刻、実行結果、ログ、例外を確認できること。
- 正常系・異常系の smoke test を用意すること。

### FR-02: Persistent state and recovery

- Prefect の実行状態を PostgreSQL に永続保存すること。
- Docker コンテナの再起動後も実行履歴が残ること。
- Server / Worker の停止・復旧時に、状態不整合を検出できること。
- リトライ・タイムアウト・失敗時の処理を設定できること。
- 同じ操作が重複実行されても、外部副作用を重複させない設計を示すこと。
- Phase 1 ではサンプルの副作用付き Task で冪等性の仕組みをテストすること。
- 注意：Prefect の retry が exactly-once execution を保証するとは仮定しない。

### FR-03: Monitoring

- Prefect UI で実行一覧、状態、実行履歴、ログ、エラーを確認できること。
- MacBook とスマートフォンから Tailscale 経由でアクセスできる構成にすること。
- 公開インターネットから Prefect UI に直接アクセスできないこと。
- Tailscale の設定、認証、アクセス制御の手順を文書化すること。

### FR-04: Notifications

- ワークフローの以下のイベントを通知できること：正常完了、実行失敗、承認待ち相当のイベント。
- 通知は HTTP ベースの Adapter として実装し、通知先・認証情報を環境変数で管理する。
- 通知失敗によって研究ジョブ自体の結果が不正に変更されないよう、通知処理とジョブの成功判定を分離する。
- 通知本文に API key、秘密情報、機密ログを含めない。

### FR-05: Deployment and operations

- Docker Compose で Prefect Server / UI、Worker、PostgreSQL を起動できること。
- PostgreSQL に永続ボリュームを設定すること。
- restart policy、healthcheck、サービス依存関係を設定すること。
- Python と Docker の依存バージョンを管理すること。
- シークレットを Git にコミットしないこと。
- 開発用・本番用の設定方法を明示すること。
- PostgreSQL のバックアップ・リストア手順を用意すること。
- ログ確認、サービス再起動、障害復旧、アップデート方法を README に記載すること。
- 本番で Docker socket や host root 権限を LLM エージェントに直接公開しない設計とすること。

## 4. Non-functional Requirements

### Maintainability

- Phase 2 以降に Agents SDK、Notion / Linear、Codex、計算バックエンドを追加できる構成にする。
- Phase 1 ではそれらの機能を実装しない。
- 既存のディレクトリ構成と命名規則を尊重する。
- 不必要な抽象化や独自フレームワークを追加しない。

### Reliability

- Worker や Server が停止したときの挙動を明確にする。
- 自動リトライと手動再実行を区別する。
- プロセスの復旧と実行中ジョブの復旧を混同しない。
- 完全な復旧を保証できないケースは制約として文書化する。

### Security

- Prefect UI / API を公開インターネットに直接露出させない。
- Tailscale を用いた限定的なアクセスを基本とする。
- 認証情報を環境変数等で管理する。
- Secret、ログ、DB、バックアップのアクセス権限を適切に設定する。
- 特権コンテナや不要な host mount を避ける。

## 5. Repository Structure（目標例）

実際の構成は既存リポジトリとの整合性を優先する。

```text
ResearchAutopilot/
├── AGENTS.md
├── CLAUDE.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── README.md
├── prefect.yaml
├── docs/
│   ├── README.md
│   ├── development_handoff.md
│   ├── phase_specs/
│   │   └── phase1.md
│   └── operations/
│       ├── deployment.md
│       ├── monitoring.md
│       └── recovery.md
├── src/
│   └── research_autopilot/
│       ├── __init__.py
│       ├── flows/
│       │   └── smoke_test.py
│       └── notifications/
├── infra/
│   ├── compose.yaml
│   └── Dockerfile
└── tests/
```

## 6. Out of Scope

- OpenAI Agents SDK の Orchestrator
- Notion / Linear API の連携
- Codex / Claude Code による自動実装
- Experiment Runner / Analyst / Reviewer
- TSUBAME / Slurm との連携
- 独自 Web ダッシュボード
- 研究タスクの自動選択
- 科学的な実験仕様の生成

## 7. Acceptance Criteria

各項目を **Passed / Failed / Not Tested** で評価する。

| ID | 条件 |
|---|---|
| AC-01 | `uv run pytest` が成功する。 |
| AC-02 | MacBook 上で正常系・異常系の Prefect Flow が実行できる。 |
| AC-03 | 研究室サーバーで Docker Compose により必要なサービスが起動する。 |
| AC-04 | サーバー上の Worker が登録済み Flow を実行できる。 |
| AC-05 | MacBook から Flow を登録・起動し、研究室サーバーで実行できる。 |
| AC-06 | スマートフォンから Tailscale 経由で Prefect UI の状態・ログを確認できる。 |
| AC-07 | Server / Worker を再起動しても、過去の Flow Run 履歴が保持される。 |
| AC-08 | 異常系 Flow で retry、failure logging、通知が機能する。 |
| AC-09 | 重複実行を模擬したテストで、副作用の重複防止が確認できる。 |
| AC-10 | README と運用ドキュメントから再構築・バックアップ・復旧の手順を再現できる。 |

## 8. Verification Constraints

- 研究室サーバー、Tailscale、スマートフォンの実環境にアクセスできない場合、実際の接続確認を成功したと報告しない。
- その場合でも、ローカルで可能な範囲の実装とテストを完了させ、サーバー側で実行する具体的なコマンドと確認方法を残す。
- 実装済みと本番環境での動作確認済みを必ず区別する。
- OS、CPU architecture、ネットワーク設定などの未確認情報を推測で確定しない。
- 本番サーバーへの破壊的変更、既存データの削除、セキュリティ設定の無断変更は行わない。
