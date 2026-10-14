# Phase 4 運用：Experiment Runner / Scientific Analyst

仕様は `docs/phase_specs/phase4.md`。Phase 3 とのインターフェースは `phase3_implementation.md` §6。

## 1. 流れ

```text
research-agent experiment run --phase3-run <run> [--phase3-run <run2> ...]    (Prefect flow "run-experiment")
  1. 契約の確認：Phase 3 の run が completed で Code Reviewer が pass、ExperimentSpec と固定 commit が一致、
     apply モードの承認と Linear の In Progress（Phase 3 と同じゲート）、spec を commit に対して再検証、
     必要な環境変数（名前のみ）がホストにある
  2. 資源：ホストの検出（cgroup / WSL / Docker / GPU）、見積もり（spec と履歴、根拠と信頼度つき）、
     管理用の予約と上限、BLAS / OpenMP のスレッド数を割り当てに合わせる
  3. スケジュール：独立した実験は上限内で並列、job_dependencies のある実験は依存先が検証に通ってから実行。
     直列にした場合は理由を serialization_notes に記録
  4. 投入（非同期）：ジョブごとに git archive で checkout し、キャッシュした venv を使う。shim（supervisor）を
     切り離して起動し、台帳には事前に記録する
  5. 監視：heartbeat は wall time の約1/3（上下限あり）。shim がメモリと wall time の上限を強制する
  6. 回収と検証：成果物を read-only にして sha256 を付ける。形式 / NaN・Inf / 単位フィールド / metrics /
     numeric checks / 入力が変更されていないか、を確認する。文章で書かれた criteria は not_verified
  7. 失敗時の振り分け：インフラ → 同じ条件で再試行、資源超過 → 資源を増やして再試行、
     実装・環境 → FailureReport を Phase 3 に渡して修正（最大2往復）→ 新しい commit と spec で再実行、
     数値・科学・不明 → blocked（人間が判断）
  8. 分析：検証済みの結果だけを LLM Analyst に渡す（単価の設定と予算が必須）
  9. 提案：Analyst の提案 → 独立した Reviewer → ScopeAssessment → ReviewedExperimentProposal
     （approval_status=pending_phase5_policy）→ Phase5Handoff
```

**Phase 4 は承認を出さない。** Notion / Linear への登録や、提案の実行も行わない（Phase 5 の担当）。

## 2. コマンド

```bash
uv run research-agent experiment run --phase3-run <phase3_run_id>            # Prefect 経由（UI に履歴が残る）
uv run research-agent experiment run --phase3-run <a> --phase3-run <b> --local
uv run research-agent experiment resume <experiment_run_id>                   # worker の再起動後に照合して続行
uv run research-agent experiment list | show <id> | proposals [--task INU-123]
```

終了コード：0 completed、2 blocked、5 failed。blocked のときは ntfy に `approval_required` を送る。

## 3. 設定（`.env`）

| 変数 | 既定値（Mac で安全な値） | Eagle の例 |
|---|---|---|
| `RA_EXP_BACKEND` | `local` | `local`（TSUBAME は `slurm`。未設定なら blocked） |
| `RA_EXP_RESERVE_CPU` / `RA_EXP_RESERVE_MEMORY_GB` | 2 / 4 | OS・Prefect・PostgreSQL・WSL の分を確保する（例：4 / 16） |
| `RA_EXP_MAX_JOB_CPU` / `RA_EXP_MAX_JOB_MEMORY_GB` | 2 / 4 | `host`（ホストの上限から予約分を引いた値）または具体的な値 |
| `RA_EXP_MAX_PARALLEL` | 2 | コア数とメモリに応じて設定 |
| `RA_EXP_MAX_WALLTIME_MIN` | 120 | 研究計算に合わせて設定 |
| `RA_EXP_MAX_RETRIES` / `RA_EXP_MAX_ROUNDTRIPS` | 2 / 2 | 同じ |
| `RA_ANALYST_MODEL` / `RA_SCI_REVIEWER_MODEL` | gpt-5.6-terra / gpt-5.6-sol | 同じ |
| `RA_MODEL_PRICING` | （なし → LLM は呼ばれず、分析は blocked） | 必須 |
| `RA_EXP_MAX_LLM_COST_USD` / `RA_EXP_MAX_PROPOSALS` | 1 / 3 | 予算に合わせて設定 |

メモリの見積もりには安全係数 1.5 を掛ける。wall time の上限は見積もりの2倍とし、`MAX_WALLTIME` で頭打ちにする。実際に空いているメモリより多くは割り当てない。

### GPU（2026-10-14 追加）

- 実験定義の `resources.gpu` が 1 以上のジョブだけに GPU を割り当てる。1 台の GPU を使えるのは同時に 1 ジョブだけ。
  - GPU が空くまで、次の GPU ジョブは待つ。GPU を使わないジョブは並列で動き続ける。
  - 理由：同じ GPU に学習を重ねるとメモリが足りなくなる。
- ジョブには `CUDA_VISIBLE_DEVICES` で自分の GPU だけを見せる（番号は nvidia-smi の順、`CUDA_DEVICE_ORDER=PCI_BUS_ID`）。GPU を使わないジョブには空文字を渡すので、GPU は見えない。
- CUDA GPU のないホストで GPU ジョブを投げると blocked になる。
- 研究リポジトリの compose イメージで動かすジョブ（`container = {...}`）に GPU を渡すには、そのリポジトリの compose.yaml に GPU デバイスの指定が必要。RA からは付けない（未対応）。
- 外部コード（Phase 9）の GPU は `phase9_external_repositories.md` を参照。

## 4. 保存先

| データ | 場所 |
|---|---|
| ExperimentManifest | `$RA_EXP_ARTIFACT_ROOT/<task>/<run>/manifest.json` と DB の `experiment_runs` |
| ジョブ台帳 | DB の `experiment_jobs`（`job_key` が一意） |
| ジョブの実行ディレクトリ | `<task>/<run>/<experiment_id>/attempt-N/`：`src/`（固定 commit を展開したもの）、`stdout.log`、`stderr.log`、`status.json`、`job.json`、`input_hashes.json`、`artifacts/`（read-only）、`failure_report.json`（修正を依頼したとき） |
| 分析・提案 | `<run>/analysis.json`、`<run>/proposals/<id>.json`、DB の `experiment_proposals`（`dedupe_key` が一意） |
| Phase 5 用の handoff | `<run>/phase5_handoff.json` |
| venv のキャッシュ | `$RA_EXP_ENV_CACHE/<env_key>`（解決したバージョンを `freeze.txt` に記録） |

秘密情報の値は、プロセスの環境として shim とジョブに渡すだけで、ファイル・DB・manifest には書かない（テストで確認済み）。

## 5. Phase 5 handoff（schema 1.0）

```text
Phase5Handoff
  experiment_run_id, task_id, phase3_run_id, final_commit, state, validations[ValidationResult],
  analysis_path, reviewed_proposal_paths[], manifest_path, created_at

ReviewedExperimentProposal
  proposal: ExperimentProposal { proposal_id, dedupe_key, parent_task_id, source_experiment_run_id,
            source_artifacts[], source_commit, chain_depth,
            draft: { title, research_question, hypothesis, purpose, implementation, acceptance_criteria[],
                     scientific_invariants[], expected_information_value, dependencies[], estimated_resources,
                     estimated_cost, risks_and_assumptions[], parameter_changes[{name, from_value, to_value, kind}] } }
  review: ScientificReviewResult { verdict: accept|revise|reject|blocked, ..., scope_within_original,
          invariants_preserved, acceptance_not_relaxed, new_scientific_judgement_required, required_changes[] } | null
  scope_assessment: { within_scope, rationale, proposed_changes[], requires_human_decision,
                      deterministic_findings[], reviewer_opinion }
  task_spec_ref, execution_plan_ref, spec_diff[], limits_metadata{chain_depth, max_chain_depth, proposals_in_run,
  max_proposals_per_run, llm_cost_usd_known, max_llm_cost_usd_per_run, estimated_resources, estimated_cost}
  approval_status: "pending_phase5_policy"   # Phase 4 は承認しない
```

`within_scope=true` になるのは、次のすべてを満たす場合だけ：決定論的なチェックで問題がない（元の invariants と acceptance をすべて逐語的に保持している、`within_spec` 以外の変更がない、invariant で固定された値に触れていない）、Reviewer が accept で、範囲・invariants・acceptance について肯定的で、新たな科学的判断が不要。それ以外はすべて `requires_human_decision=true`。

## 6. 失敗時の振り分け

| 状況 | 分類 | 対応 |
|---|---|---|
| ディスク / ネットワーク / I/O 系のエラー | infrastructure_failure | 同じ条件で再試行（上限・backoff あり） |
| shim がメモリ / wall time 超過で停止、SIGKILL | resource_exceeded | 資源を1.5倍にして再試行（上限内なら） |
| 状態が分からない（LOST） | infrastructure_failure | **自動で再投入しない** → blocked |
| exit 0 だが必須の出力がない / 読めない | implementation_failure | Phase 3 で修正 |
| リポジトリ内のコードで例外 / リポジトリ内のモジュールの import に失敗 | implementation_failure | Phase 3 で修正 |
| 外部の依存の import 失敗、ライブラリ不整合 | environment_failure | Phase 3 で修正 |
| NaN / Inf、収束しない、特異行列など | numerical_failure | blocked |
| numeric checks / metrics の不合格 | scientific_validation_failure | blocked |
| ジョブが scientific な入力を書き換えた | implementation_failure | blocked |
| それ以外 | unknown | blocked |

Phase 3 に渡す修正範囲は、元の承認範囲から、scientific な入力に当たる glob と experiment.json を除いたもの。Phase 3 側でもさらに検証する（`implementation/failure.py`）。

## 7. Eagle に反映するときの手順（今回は実施しない）

1. worker イメージ：研究リポジトリの依存をインストールするための `uv` と、Phase 3 で使う CLI。Docker の中で動かす場合、**コンテナを再起動すると shim と実行中のジョブも止まる**ので、resume で LOST として検出したあと、人間が判断する。長時間の計算では、ホスト側で動く worker（WSL2 のプロセス）も検討する。
2. volume：`RA_EXP_ARTIFACT_ROOT`、`RA_EXP_ENV_CACHE`、研究リポジトリ、`RA_WORKTREES_DIR`。
3. `.env`：`RA_EXP_*` の上限を Eagle に合わせる（予約分を必ず残す）。WSL2 では `.wslconfig` の memory / processors が上限になる（`detect_host` が WSL を検出して記録する）。`RA_MODEL_PRICING`。
4. `prefect deploy --all`（`run-experiment/manual`）。研究計算を受け付けるのは、上限と予約を確認してから。
5. TSUBAME：`SlurmConfig`（host、partition、account、remote_root、transfer、max_walltime、policy_acknowledged）と SSH の実行方法を人間が決めるまでは blocked のまま。
