# AGENTS.md — Research Autopilot 共通開発ルール

このファイルは Codex・Claude Code など、このリポジトリで作業するすべてのコーディングエージェント共通の開発指示です。
開発ルールはこのファイルに一元化します（`CLAUDE.md` はこのファイルを読み込むだけです）。

---

## 1. プロジェクトの目的

**Research Autopilot** は、研究者が定義・承認した研究開発タスクを、複数の専門エージェントが自律的に実行するマルチエージェントシステムです。

- 対象分野：機械学習・量子化学（現在の主テーマは Active-space selection＝量子化学計算に使う活性軌道空間の選択）
- 目標：研究者が Notion / Linear にタスクを登録・承認すると、システムが依存関係に従って実装・実験・解析を進め、結果を記録する。
- 人間の役割：研究方針の決定、科学的仕様の承認、重要な結果の検証。

## 2. 目標アーキテクチャ（全体像）

```text
User
  -> Notion / Linear
  -> Workflow Engine (Prefect)
  -> Research Orchestrator (OpenAI Agents SDK)
       +-- Task Manager       Notion / Linear から仕様・状態・依存関係を取得
       +-- Implementer        Codex 等で実装・テスト
       +-- Reviewer           コード品質・テスト・科学的制約を検証
       +-- Experiment Runner  計算ジョブを実行・監視
       +-- Analyst            実験結果を集計・解析・検証
  -> Notion / Linear に結果を反映
```

外部システム：Notion（Research Tasks / Research Notes）、Linear（実行状態・依存関係）、Git、Prefect、Codex / Claude Code、研究室 Linux サーバー（本番の常時稼働ホスト）、TSUBAME（HPC）。
開発は MacBook、本番は研究室サーバー上で Docker Compose により稼働させる想定です。

### アーキテクチャの基本原則

1. **構造化された連携**：エージェント間は自由形式の会話ではなく、型付きの入出力（例：Pydantic モデル）と成果物ファイルで連携する。
2. **状態は会話の外に置く**：長時間実行の状態は Workflow Engine と永続ストレージで管理し、エージェントの会話履歴に依存しない。
3. **再開可能性**：どの処理も中断・再起動から復旧できるよう、冪等性と checkpoint を意識する。
4. **人間の承認ゲート**：科学的仕様の変更や重要な判断は人間の承認を経る。
5. **外部仕様を仮定しない**：Notion / Linear は将来の外部 SSOT だが、現時点でエージェントは直接アクセスできない。外部の仕様を取得できると仮定せず、ローカルに明示的に提供された仕様書を入力とする。

### ロードマップ（背景情報）

| Phase | 内容 |
|---|---|
| 1 | Execution Infrastructure：Prefect 実行基盤、Docker Compose デプロイ、状態永続化・復旧、スマホからの状況確認、通知 |
| 2 | Task Manager / Orchestrator：Notion / Linear 連携、仕様・依存関係取得、Agents SDK による計画・委譲 |
| 3 | Implementer / Reviewer：Codex による実装、Git worktree 隔離、テスト・レビュー・修正、commit |
| 4 | Experiment Runner / Analyst：研究室サーバー / TSUBAME (Slurm) での計算、非同期監視、解析、再現性情報 |
| 5 | Autonomous Research Workflow：承認済みタスクの連続自律実行、自動更新、承認フロー、予算制限、E2E 検証 |

各 Phase の詳細要件は、その Phase の実装依頼時にユーザーが提供し `docs/phase_specs/phaseN.md` に格納されます。**仕様書が存在しない Phase の詳細をエージェントが独自に定義してはいけません。**

## 3. プロジェクト構成と命名規則

現状の構成（2026-10-10 Phase 1 実装時点）：

```text
.
├── AGENTS.md                  # 共通開発ルール（このファイル）
├── CLAUDE.md                  # Claude Code 入口（@AGENTS.md のみ）
├── README.md                  # 概要・クイックスタート・運用早見表
├── .gitignore / .dockerignore
├── .env.example               # 環境変数のひな形（実値の .env は Git 管理外）
├── pyproject.toml / uv.lock   # uv 管理、Python >= 3.12、プロジェクト名 research-autopilot
├── prefect.yaml               # Prefect deployment 定義
├── src/
│   └── research_autopilot/    # 唯一のパッケージ（entry point: research_autopilot:main）
│       ├── flows/             # Prefect flow（Phase 1: smoke_test.py）
│       ├── tasks/ planning/ orchestrator/ storage/ cli.py   # Phase 2
│       ├── implementation/    # Phase 3（Implementer / Reviewer / ExperimentSpec / FailureReport）
│       ├── experiments/       # Phase 4（Runner / Resources / Validator / Triage / Analyst / Scope）
│       ├── autopilot/         # Phase 5（Policy gate / Approvals / Outbox / Engine / Completion）
│       ├── catalog/           # Phase 7A（Artifact Catalog / Adapter lifecycle / declarative engine）
│       ├── notifications/     # HTTP 通知 adapter と state hook
│       └── idempotency.py     # 冪等な副作用
├── infra/                     # compose.yaml / Dockerfile / automations/
├── tests/                     # pytest
└── docs/
    ├── README.md              # docs 運用ルール
    ├── development_handoff.md # 引き継ぎ記録
    ├── phase1_technical_design.md
    ├── operations/            # deployment / monitoring / recovery
    └── phase_specs/           # Phase 別仕様書（phase1.md〜phase5.md、phase6b.md、phase7*.md）
```

規則：

- Python パッケージは `src/` レイアウト、モジュール名は snake_case。
- パッケージ名は `research_autopilot`、配布名・CLI コマンド名は `research-autopilot` に統一する。新しいトップレベルパッケージを増やさない。
- テストは `tests/` に `test_*.py` として置き、pytest で実行する。
- 依存関係は `uv add` / `uv remove` で管理し、`pyproject.toml` と `uv.lock` を一緒に commit する。依存の追加・削除は対象 Phase の仕様またはユーザー指示がある場合のみ。
- ディレクトリの再編成は、仕様またはユーザー指示がない限り行わない。

## 4. 作業開始時の確認手順

セッションを開始したら、コードを変更する前に必ず以下を行う。

1. `git status` と `git branch --show-current`、`git log --oneline -10` で作業ツリーの状態を確認する。未 commit の変更があれば、誰の何の作業かを引き継ぎ記録と照合し、勝手に破棄しない。
2. `docs/development_handoff.md` を読み、Current Phase / Status / Exact Next Actions を把握する。
3. 対象 Phase の仕様書 `docs/phase_specs/phaseN.md` を読む。
4. **仕様書が存在しない、または必要な情報が欠けている場合は、推測で実装を始めずにユーザーに確認する。**
5. 既存のテストを実行し、作業前の状態（成功・失敗）を把握する。

## 5. 要件・仕様の優先順位（Source of Truth）

1. ユーザーが明示的に提供した最新の仕様・指示
2. `docs/phase_specs/` の対象 Phase の仕様書
3. `AGENTS.md`（このファイル）の共通開発ルール
4. 既存コードとテスト
5. `docs/development_handoff.md` の作業状況

- 仕様と既存コードが矛盾する場合、黙ってどちらかを変更せず、ユーザーに報告する。
- 引き継ぎ記録は**進捗の記録であり、仕様の正本ではない**。
- 開発中に判明した事実や設計判断は、仕様書を書き換えずに引き継ぎ記録（または `docs/README.md` で定める記録先）に残す。仕様書の変更はユーザーの承認を得てから行う。

## 6. Git と commit の運用

- 作業ブランチを切って作業する（例：`phase1/<topic>`）。`main` への直接 commit はユーザーが許可した場合のみ。
- **意味のある実装単位ごとに**テストを実行し、commit する。巨大な一括 commit を避ける。
- commit メッセージは「何を・なぜ」が分かる簡潔な要約（英語または日本語）。関連 Phase を明示する（例：`phase1: add flow run persistence`）。
- 無関係なファイルの変更・フォーマットを混ぜない。
- `git push --force`、`git reset --hard`、履歴書き換え、ブランチ削除などの破壊的操作はユーザーの明示的な許可なしに行わない。
- 秘密情報・生成物・大きなデータ・仮想環境（`.venv/`）を commit しない。除外対象は `.gitignore` に追加する。

## 7. テスト・検証の原則

- 新しい機能には pytest のテストを伴わせる。テスト実行は `uv run pytest`。
- 報告では次の3つを必ず区別する：
  - **実装済み・検証済み**：実際にテスト・コマンドを実行し、結果を確認した。
  - **実装済み・未検証**：コードはあるが実行・テストしていない。
  - **未実装**。
- **実際に実行していないテストを成功したと報告しない。** 失敗したテストは出力とともにそのまま報告する。
- 外部サービス（Notion / Linear / TSUBAME 等）が必要で検証できない部分は、モック等で何を確認し、何が未確認かを明記する。
- 科学計算の結果は、数値の妥当性（既知の参照値、単位、収束）を確認できた範囲を明記する。

## 8. 引き継ぎ（セッション中断・終了時）

Claude と Codex は交互に作業するため、**会話履歴に依存せず開発を再開できること**を前提とする。

- 引き継ぎ記録は `docs/development_handoff.md` の1ファイル。
- 更新タイミング：
  - 重要な実装単位が完了したとき（commit と同時が望ましい）
  - セッション終了時・中断前（必須）
  - ブロッカーが発生したとき
- 記入内容は同ファイルのテンプレートに従う。特に **Exact Next Actions** は、別のエージェントが読んで即座に着手できる具体性で書く（ファイル名、コマンド、判断待ちの事項）。
- 事実のみを書く。未確認の事柄は「未確認」と明記する。
- 前のエージェントの記録を上書きで消さない。古い Completed Work は要約してよいが、未解決の Known Issues は解決するまで残す。

## 9. セキュリティ・認証情報管理

- API キー、トークン、パスワード、SSH 鍵などをコード・ドキュメント・commit・ログ・引き継ぎ記録に書かない。
- 認証情報は環境変数または `.env`（Git 管理外）で扱い、必要な変数名だけを `.env.example` 等で示す。
- 秘密情報を含む可能性のあるファイルを見つけた場合は commit せずにユーザーに報告する。
- 外部サービスへの接続設定の追加は、対象 Phase の仕様またはユーザー指示がある場合のみ。

## 10. 自律実行時の安全制約

- **人間の承認なしに科学的仕様（計算条件、評価指標、データセット、acceptance criteria 等）を変更しない。**
- 既存の仕様書・実験結果・成果物を無断で変更・削除しない。
- 研究室サーバーや TSUBAME など共有計算資源へのジョブ投入、長時間・高コストの処理、課金を伴う API 呼び出しは、仕様で許可された範囲とユーザーの承認の範囲内でのみ行う。
- 本番環境（研究室サーバー）への直接デプロイ・設定変更は、ユーザーの明示的な指示がある場合のみ。
- 不確実な点・判断が必要な点に当たったら、推測で進めず、引き継ぎ記録の Known Issues / Blockers に残してユーザーに確認する。
