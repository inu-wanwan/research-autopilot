# Phase 4 仕様 — Autonomous Experiment Runner & Scientific Analyst

- 作成：2026-10-10 / Claude Code（ユーザー指示により作成）
- 出典：ユーザーが提示した「ResearchAutopilot Phase 4 — Autonomous Experiment Runner & Scientific Analyst」（§Objective、§1〜§12）。Notion は参照しない（指示どおり）。2026-10-10 のユーザー回答（§0）。
- 変更はユーザーの承認がある場合のみ。

## 0. 確定事項と調査結果（2026-10-10）

| 論点 | 決定 / 事実 |
|---|---|
| 既存の supervisor | リポジトリに **supervisor は存在しない**。LocalBackend の中に、切り離して起動する job wrapper（`experiments/shim.py`）と永続のジョブ台帳を実装し、これを supervisor の役割とする |
| 実 LLM（Analyst / Reviewer）の費用 | ユーザーが `.env` の `RA_MODEL_PRICING` に単価を設定し、予算上限の範囲で実行する。単価が分からないモデルは呼ばない（blocked） |
| モデル | Analyst は `gpt-5.6-terra`、Scientific Reviewer は `gpt-5.6-sol`（Phase 2 の方針と同じ。設定で変更可） |
| Mac での資源 | M2、8コア、16 GB、GPU は Metal のみ（CUDA なし）。既定で研究ジョブは合計 CPU 2 / RAM 4 GB までとし、管理用に資源を確保する。Eagle 相当の高負荷設定は Mac では有効にしない |
| 実行承認 | Phase 3 の run が完了（Code Reviewer が pass）、ExperimentSpec と固定 commit が一致、Phase 2 の apply 記録があり、Linear が In Progress（Phase 3 のゲートを再利用）。どれかが欠ければ blocked |
| TSUBAME | SlurmBackend は interface と sbatch script 生成、Fake Slurm によるテストまで。接続・投入はしない。設定が不足していれば blocked |

## 1. 構成要素

- **Prefect Experiment Orchestrator**：受け取り、権限の検証、状態の遷移、submit / monitor / collect / validate / analyze / review、再試行、履歴、Phase 5 への handoff。
- **ComputeBackend**：`submit`、`status`、`cancel`、`collect`、`recover`。実装は LocalBackend、SlurmBackend、FakeBackend。
- **Resource Manager**：資源の検出、見積もり（根拠と信頼度つき）、予約、監視、スレッド数の設定、上限の強制。
- **Deterministic Validator**：成果物、形式、NaN / Inf、metrics、numeric checks、入力の整合性を決定論的に検証する。検証できないものは not_verified とし、pass にはしない。
- **Failure Triage**：分類（Phase 3 の `ErrorClass` を使う）→ 同じ条件で再試行 / Phase 3 で修正 / blocked。
- **LLM Scientific Analyst** と **独立した LLM Scientific Reviewer**（OpenAI Agents SDK）。
- **ScopeAssessment**：決定論的な差分と Reviewer の見解から作る。曖昧なら `requires_human_decision=true`。

## 2. 主な方針

- 入力は、Phase 3 の Phase4Handoff と ExperimentSpec（1.0）、ImplementationManifest。
- 実行環境：
  - 固定 commit の `git archive` をジョブごとの実行ディレクトリに展開する（immutable。過去の成果物は上書きしない）。
  - 依存環境は、dependency files の hash をキーにしたキャッシュ venv（uv）。解決したバージョンを記録する。
  - 実行はシェルを経由せず argv で行う。秘密情報は名前だけを受け取り、値はホストの環境から渡して保存しない。スレッド数を指定する環境変数で oversubscription を防ぐ。
- ジョブ台帳：一意な投入キーで submit 前に記録してから起動する。job ID を保存する前にクラッシュしても、実行ディレクトリの marker と pidfile で状態を照合する。状態が分からないジョブは無条件に再投入しない。
- heartbeat の間隔：見積もった wall time の約1/3を目安に、下限と上限を設ける。
- Phase 3 への差し戻し：FailureReport を `implementation.orchestrator.repair` に渡す。往復は最大2回で、回数は記録する。科学に関わる変更は自律的には行わない。
- 提案：ExperimentProposal → Reviewer → ScopeAssessment → ReviewedExperimentProposal → Phase5Handoff。**Phase 4 は承認を出さない**（`approval_status = "pending_phase5_policy"`）。Notion / Linear への書き込みや実験の起動もしない。

## 3. Acceptance（ユーザー指示 §10・§12 の要約）

Fake を使った unit / contract テスト（schema、資源、並列と依存、ComputeBackend の5操作、重複 submit、worker の再起動、成果物の破損と NaN、制約違反、再試行、Phase 3 での修正と2往復の上限、予算、提案の審査と重複防止）と ScopeAssessment 専用のテスト。Mac の LocalBackend による実ジョブ（正常終了、並列、依存の順序、故意の失敗 → Phase 3 で修正 → 再実行、worker の再起動からの復旧）。実 LLM の結果と Fake の結果は区別して報告する。Phase 1〜3 の regression。Phase 5 への着手、Eagle へのデプロイ、main への merge はしない。
