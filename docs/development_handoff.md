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

Phase 10（Autonomous Research Planning & Orchestration、10A / 10B）と watchdog まで実装済み。**2026-10-13 ユーザー指示で `phase10/research-planning` を main に fast-forward し、main とブランチを origin に push 済み（`5c7da88`）**。研究室サーバーへのデプロイは未実施（ユーザーが行う予定、手順は `docs/operations/deployment.md`）。Eagle と TSUBAME は未実施。

## Current Task

INU-292（[E2E] H₂ RHF–FCI 結合解離曲線の自律研究）で 10A→10B の E2E を進めている。詳細手順・現状・再開方法は `docs/operations/phase10_research_orchestration.md` の「E2E 実施記録（INU-292）」と「引き継ぎ：E2E の続け方」。

## Status

10A / 10B 実装済み、`uv run pytest` 521 passed（`810986e` 時点。以後の小修正は `tests/test_phase10_research.py` 32 passed / 関連テストで確認）。E2E は承認 v3（計画 v6）でサイクル 4（INU-301 計算再実行 → INU-302 解析）が実行中（2026-10-11 13:3x UTC 時点）。Phase A（INU-274）の 10A dry-run は不足情報（予算・baseline 手法等）を blocking として検出し計画を作らないことを確認済み。

## Last Updated

2026-10-13 05:xx JST / Claude Code

## Git Branch / Commit

- 作業ブランチ：`phase7/artifact-catalog`（main `2f662bc` から分岐）
- Phase 7B commit：`546c9b8`（生成・検証・sandbox）→ `2c53c93`（feedback 改善・trust policy・粒度 gate）→ `ffd47c8`（docs）→ 自律運用 commit。main に fast-forward merge 済み（push 未実施）
- Phase 7A commit：`b66fd2c`（仕様）→ `e1b5782`（core）→ `41bb60d`（連携）→ `a711a33`（CLI）→ docs commit
- Phase 6B：`codex/phase6b-parent-dag` を main に fast-forward 済み
- Discord 実装 commit：`d5e3138`
- Gateway fail-closed 修正 commit：`a294624`
- 結果 UX 改善 commit：`800ac7f`、`fa19f3a`
- Phase 5 の既存 commit：`d52c5fe` → `9c41066` → `acb0bcf` → `2953d60` → `6959575` → `f8dd138` → `51d493e`

## Completed Work

### Eagle デプロイ構成：WSL ホスト実行（2026-10-13 / Claude Code、ユーザー決定）

- ユーザー決定（2026-10-13）：Phase 3 以降は WSL ホスト上のプロセスで動かす。Phase 9 は無効にしない。
- 理由：Eagle の `rixyt` は docker グループ（root 相当）。LLM 生成コードを rixyt で動かすと Phase 1 仕様 §3 に反する。そこで次の構成にした。
  - 専用ユーザー `ra`（docker / sudo なし）を作る。
  - `ra` 専用の rootless Docker で Phase 9 を動かす。
  - systemd --user で worker / Discord bot / watchdog を常駐させる。
  - compose は postgres / prefect-server だけを動かす。
- 追加したもの：
  - `infra/compose.host.yaml`：postgres を `127.0.0.1:5432` に publish し、container worker / bot を profile `container-worker` に入れる。
  - `infra/host/systemd/*.service`、`infra/host/with_dotenv.py`、`.env.example` の host 節。
  - `docs/operations/eagle_wsl_setup.md`：人間の操作に絞った手順。
- `external/sandbox.py`：rootless Docker を検出したら、bind mount に書く container（wheel 取得・承認済み研究実行）を `--user 0:0` で動かす。rootless では host uid が subuid に写り、書けなくなるため。検証用の実行は nobody のまま。
- 検証済み（Eagle 上）：
  - WSL から Docker Desktop の `127.0.0.1` の publish ポートに届く（使い捨て postgres コンテナで確認）。
  - compose override の `config --services` が postgres / prefect-server だけになる。
  - `systemd-analyze verify` が通る。
  - with_dotenv で JSON 値の読み込みと、既存の環境変数を優先することを確認した。
  - `tests/test_phase9_external.py` 42 passed。
- 未検証：H2〜H5 の通し実行、ra の rootless Docker で Phase 9 の制限オプションが通るか、ra での Codex / Claude ログイン。
- WSL の rixyt に uv 0.12.18 を `~/.local/bin` に入れた（テスト実行のため）。
- 2026-10-14：人間が H1〜H4 を完了（systemd 有効、`ra` は uid 1002・groups ra,users のみ、`/opt/research-autopilot` は ra 所有）。
- 2026-10-14：エージェントが A1 を実施した。
  - rixyt 用チェックアウトは `~/workspace/ResearchAutopilot`（`/mnt/c` の repo から clone、LF、`.env` 600、`GIT_SHA=feed112`）。今後はこちらで作業する。`/mnt/c` 側は CRLF のため commit に使わない。
  - compose（host override）で postgres / prefect-server が healthy。既存 volume のパスワードと一致していることも確認した。
  - `research_autopilot` DB を作成し、`db init` で 37 table。
  - Windows 側の待ち受けは 4200 / 5432 とも 127.0.0.1 のみ。LAN・tailnet の IP からの直接接続は不可。tailnet の Serve は `/api/health` 200、認証なしは 401。
- 2026-10-14：ユーザー指示でエージェントが `C:\Users\rixyt\.wslconfig` を作成した（`memory=52GB`、`processors=32`。計算ノードとして使うため）。
  - 反映には人間の `wsl --shutdown` と Docker Desktop の再起動が必要。反映後の `free -g` は未確認。
  - 合意した `RA_EXP_*` は `eagle_wsl_setup.md` H5 にある。
  - 未決定：`RA_AUTOPILOT_BUDGET_CPU_HOURS`。
  - 既知の注意：`ra-prefect-worker` の restart は実行中のジョブも止める（KillMode が既定のまま）。
- 判明したこと：`.env` を `set -a; . ./.env` で shell に読み込むと、`RA_MODEL_PRICING` の JSON 行でエラーになる（値は読まれない）。`deployment.md` §2 の automation 登録手順が該当する。代わりに `uv run python infra/host/with_dotenv.py <cmd>` を使う。

### Phase 10 Research Planning / Orchestration（2026-10-11 / Claude Code）

- 実装：`src/research_autopilot/research/`（models / scope / evidence / planner / workflow / evaluator / orchestration / cli）。既存 Phase 5 engine の拡張（`_authorize_research`、`WorkflowStage.RESEARCHING / PAUSED`、`ApprovalKind.RESEARCH_SCOPE`）、6B ParentRun を研究サイクルに再利用（`ParentRun.research_cycle_id`）、Discord Stop / Resume（`DiscordControlInput`）、Notion `Origin` / `Approval Status`、Linear blocked-by（`create_relation`）。
- E2E で見つかり修正した不具合（commit `ac0af84`〜`a7e230f`）：strict JSON schema 非対応の出力型、範囲境界の未抽出、初期計画への結果依存タスク混入、max_count 条件の誤用、RA 既定予算の明示、自律上限の数値照合（日本語数字境界・「以上」は上限にしない）、Phase 4 validator のネスト／ワイルドカード対応、子タスク仕様へのスコープとサイクル全体パラメータの記載、進展なし判定の範囲（現承認かつ最後の Resume 以降）、Resume 時の再評価と HUMAN_RESUME の Evaluator への提示、通知整理（定型は logs、ボタンは results/errors のみ、Resume 左）。


### Phase 9 External Repository Onboarding（2026-10-11 / Claude Code）

- `src/research_autopilot/external/`：識別（official / author / third_party / unknown と根拠・不確実性）、commit 固定の tar.gz 取得と Git blob 照合、決定論的ライセンス判定、静的依存調査、静的安全性スキャン、Docker サンドボックス（binary wheel のハッシュ固定・オフラインビルド・ネットワークなし検証）、LLM テスト計画（失敗を最大 3 試行まで差し戻し）、Registry（DB 正＋Catalog `ra-external`）、Discord 実行承認と実行時ゲート、研究パッチ（hash 単位承認・サンドボックス再検証）、実行記録。
- 連携：Phase 2（参照実装の登録・Planner 文脈・handoff `external_implementations`・費用計上）、Phase 3（`external_code` 宣言の規則と検証）、Phase 4（承認済みコンテナでのみ実行、未承認は Blocked＋承認依頼、結果に provenance）、Phase 5（poller が判断を適用し run 再開、24h ごとの更新再検証）。
- 検証：`uv run pytest` 489 passed（Phase 9 は 39 件）。BOCS の E2E 工程 1〜10 を実サービスで実施（docs 参照）。


### Phase 8 Literature Research（2026-10-11 / Claude Code）

- ユーザー決定：推奨案すべて（依存 `playwright` / `pypdf`、arXiv・Crossref・OpenAlex・Unpaywall・GitHub、許可リスト、購読論文は自動取得しない、PDF は Zotero/Drive と非公開キャッシュのみ、Zotero コレクション `ResearchAutopilot`、Planner 連携、`gpt-5.6-sol`・$2/調査、Notes 分類「文献」、E2E は BOCS）。連絡先 `RA_LITERATURE_CONTACT_EMAIL` を `.env` に追加。
- PoC 調査：`~/workspace_callduck/dev/zotero-connector-poc`（58b3871、判定 PARTIALLY_FEASIBLE、Mac 検証済み・Windows 未検証）。方式 C を `literature/zotero/` に移植し、Connector 5.0.217 を `~/.research-autopilot/zotero/connector` に固定。
- 検証済み：`uv run pytest` 445 passed（Phase 8 18 件）。実データ：BOCS 発見・Zotero 保存・repo snapshot（fef0d4e）・66 主張中 63 件照合・矛盾 5 件・Catalog verified・Notion Note 作成、実 Planner が保存済みレポートを再利用し S1/S2 を引用（Reviewer 差し戻しで Blocked、試験仕様が簡素なため）。費用 BOCS 調査 $0.52 + Planner $0.32 + 失敗した 1 回分（未記録）。
- commit：`022ca30`（仕様）→ `176a3b6`（検索・取得・本文）→ `cf7f9e4`（Researcher・Catalog・Notes・Planner）→ docs commit。


### Phase 7B Autonomous Adapter Generation（2026-10-11 / Claude Code）

- `catalog/generation.py`（detect、OpenAI generator、autovalidate、有効化判断、`catalog_generations` 記録、通知）、`catalog/survey.py`、`catalog/sandbox.py`、CLI `catalog detect|generate|generations|adapter endorse`、`RA_ADAPTER_GENERATOR_MODEL` / `RA_ADAPTER_GENERATION_BUDGET_USD`。
- ポリシー：generated declarative は全 gate 合格かつ人間作成 Adapter を置き換えず verified を減らさない場合のみ自動有効化。generated python は macOS `sandbox-exec`（Linux は bwrap、未検証）でのみ実行し、人間の有効化が必須。generated Adapter の artifact も既定で再利用（ユーザー決定、科学的妥当性は下流 Reviewer の責務。`trust=human` で限定可）。Phase 2 が要求された repository の Adapter 生成・再 scan を自動実行し CLI 操作不要（python Adapter は自律導入しない）。
- 検証済み：`uv run pytest` 424 passed / 1 dependency warning（Phase 7B 24 件を含む）。sandbox の書き込み・外部読み取り・network・subprocess・fork 拒否を Mac で確認。実モデル（`gpt-5.6-sol`、計約 $2.13）：合成 3 構造で自動有効化、active_space_research でも自動有効化。初回は全提案が gate で拒否され、feedback 改善後に成功。生成 Adapter は手作りより弱い check で旧 run を verified にしたため trust policy を導入。詳細・未検証事項は `docs/operations/phase7b_adapter_generation.md`。
- 実モデル試行の作業用 DB・合成 repository は scratchpad のみ（本番 DB・研究 repo は不変、研究 repo は clean）。

### Phase 7A Artifact Catalog（2026-10-11 / Claude Code）

- 仕様 `docs/phase_specs/phase7.md` / `phase7a.md` / `phase7b.md` を保存（ユーザー提供）。
- `src/research_autopilot/catalog/`：repository-agnostic な core（identity / metadata / provenance / content hash / validation status / 環境別 location）、Adapter lifecycle（register → validate → activate、version 不変、retire、binding は Adapter 名）、データのみの declarative engine（pattern confinement、symlink 非追跡、固定語彙の check、cross_reference）、名前付き validator、CLI `research-agent catalog ...`。DB に `catalog_*` 4 table。
- 連携：Phase 2 が Input artifacts の `catalog:` 行を Planner 前に解決（不可なら `DATA_INCONSISTENCY` で BLOCKED）し `ExecutionHandoff.catalog_resolution` に凍結、Phase 3 は read-only 入力としてプロンプト提示、Phase 4 は job 直前に再解決・再ハッシュし一致時のみ `RA_CATALOG_INPUTS` を渡す。
- `config/catalog/adapters/active-space-phase4-orbital.json`（repository 固有知識はここだけ）。`RepoConfig.locations`、`RA_CATALOG_ENVIRONMENT` を追加。
- 検証済み：`uv run pytest` 401 passed / 1 dependency warning（Phase 7A：catalog 17、integration 7）。実データ：204 artifact 発見、`supervisor_6c0d1dd` 81 件 verified（52,312 評価値を base cache と照合）、6c17c30 81 件・3a61c10 42 件は inconsistent、commit 未指定の 81 条件要求は ambiguous で 0 件、`git_commit=6c0d1dd` 指定で 81 件解決。研究 repo は read-only（検証前後で clean）。詳細は `docs/operations/phase7a_catalog.md`。
- 未検証：本番 PostgreSQL での登録、Eagle での scan / 他環境 location、`catalog.builtin` の python Adapter（未作成）。

### Phase 6B Parent Task DAG Execution（2026-10-11 / Codex）

- ユーザー提供仕様を `docs/phase_specs/phase6b.md` に保存。Notion `親タスク` / `サブタスク` relation を reader model に追加し、双方向 identity、Linear project、scope、canonical scientific/implementation spec を fail-closed で検証する。
- version/hash 付き `TaskDAG`、`ParentRun`、`ArtifactHandoff` と PostgreSQL tables を追加。子は既存 ExecutionRequest / ExecutionRun / WorkflowRun と Phase 2〜5 を再利用し、request source `parent_dag` と parent/dag metadata で関連付ける。
- Linear `blocked-by` だけを executable edge とし、A番号や prose から依存を推測しない。cycle、DAG 外 dependency、duplicate、circular parent、cross-project、identity、repository/spec/acceptance を検証する。
- Poller は process restart 後も ParentRun を復元し、ready node だけを schedule する。global concurrency と同一 repository serialization を適用。artifact は producer/consumer/path/SHA-256/commit/schema/scientific metadata/validation status を保存し、schedule 時と実行直前に再検証する。
- 親 status を child から導出し、Notion/ExecutionRun に同期。全 mandatory child 完了後に deterministic artifact evidence 付き Parent Completion Reviewer を実行し、不成立なら既存 Discord Completion Review、成立後は Notes / Linear Done / Notion Completed を行う。
- 実 Notion / Linear read-only dry-run：INU-274 の4子 relation、identity、同一 project は正常。A1〜A4 の Linear blocked-by は0件。親 Acceptance Criteria、各子 `autopilot` label / canonical Specification / Repository が不足し、4-node DAG hash `bbfd39b...237a` は Blocked。本番 task は実行・変更していない。詳細は `docs/operations/phase6b_parent_dag.md`。
- 着手前 baseline：`uv run pytest` は 355 passed。実装後の全回帰は 362 passed / 1 dependency warning。子 Prefect URL の最終追加後に Phase 6A/6B 13 tests と `git diff --check` が成功。ruff は dev dependency に存在せず未実行（`python -m compileall -q src tests` は成功）。
- Phase 6B follow-up：Linear blocked-by 未設定時の Dependency Planner を追加。仕様中の evidence と artifact contract を持つ候補 DAG を生成し、unknown/self/duplicate/cycle/根拠不在を決定論的に拒否する。DAG hash の Discord 承認前は子を開始せず、承認待ちの source spec/relation 変更は stale → Blocked。`RA_DAG_INFERENCE_MODEL` は未設定時 reviewer model。新規 Phase 6B tests 11件が成功し、ユーザー指示により全回帰は受け入れ時まで未実行。
- Phase 6B 追加（2026-10-11 / Claude Code）：Dependency Planner を commit `7042f76`。§12 の不足テスト（global slot 並列、同一 repository 直列化、child approval 待ち中の sibling 継続、child Failed / scientific invariant Blocked、partial DAG recovery）を `58f29e2` で追加。失敗隔離を実装：child が Failed/Blocked でも独立 sibling の管理と ready node の schedule を続け、進行可能 node がなくなってから親を終端化し、child の原因を親 `Execution Error` に集約する。あわせて「親 Awaiting Approval 中に child が Failed → `ValueError` で poll が落ちる」既存バグを Running 経由の遷移で修正。検証済み：`uv run pytest tests/test_phase6b_dag.py` 18 passed、`uv run pytest` 373 passed / 1 dependency warning、`git diff --check` 成功。child の Failed/Blocked は store 上の状態操作で再現しており、実際の scientific invariant 違反から Phase 4 経由で Blocked になる E2E は未確認。`OpenAIDependencyInferer` の実モデル呼び出しは未検証（課金 API のため未実行）。
- Phase 6B 追加2（2026-10-11 / Claude Code）：DAG intake に親 `autopilot` scope、Repository の registry 登録、非 canonical Repository 見出しのヒント、`Allowed files` 必須を追加（`36684a5`、`363984f`）。検証済み：`uv run pytest` 377 passed / 1 dependency warning。Phase A の Notion / Linear / registry 反映内容は `docs/operations/phase6b_parent_dag.md`「Phase A 準備状況」。

### Phase 6A Explicit Execution Control（2026-10-11 / Codex）

- `autopilot_execution_requests` と `autopilot_execution_runs` を追加。Notion の `Run Requested=true` を poller が検出し、spec / Notion-Linear 対応 / write safety gate を通過した後だけ DB に request と run を保存する。`TaskManager.next_candidates()` による label-only 起動は poller から除去した。
- DB 保存後にだけ `Run Requested` を clear し、Notion への status projection は Linear の既存 `Status` mirror と別プロパティだけを PATCH する。checkbox には event ID / CAS がないため、page id + `last_edited_time` の一意性、global poll lease、active-run guard で安全側に倒す。
- 実 Notion data source を read-only で確認：`Run Requested` checkbox、`Execution Status` status、`Last Run` url、`Execution Error` rich_text、既存 `Status` status は存在。`Run Autopilot` Button は integration から未検出。Notion API は Button を create/configure/press できないため、手動 UI 設定手順を `docs/operations/phase6a_execution_control.md` に記録。
- 検証済み：`uv run pytest tests/test_phase6_execution.py tests/test_connectors.py` は 23 passed。Phase 5 regression 41 tests は 38 passed / 3 failed。2件は Phase 5 の「follow-up child を label だけで自動起動」期待で、Phase 6A の明示起動不変条件と意図的に非互換。1件は test-only 期待が旧 WorkflowRun の Blocked 作成を期待するため（現実装は DB acceptance 前に Notion Blocked を投影）。
- 後続修正：子タスクも explicit request に変更、Completed task rerun、Prefect flow URL の `Last Run` 投影、Blocked request のDB履歴とacknowledgement、古いrunによるRequested上書き抑止、cancel DB/Notion projectionを追加。`INU-279` の人間参加E2Eは Button → Requested → Awaiting Approval → Discord approve → Implementing → Experimenting → Completed / Linear Done / Notion Completed まで実サービスで確認済み。修正後の Last Run URL は次回実行で確認が必要。

### Definition of Done（2026-10-12 / Claude Code、ユーザー承認の設計変更）

- 背景：INU-303 の Phase 3 で Reviewer が仕様外の要件で差し戻しを繰り返した（6 試行中 5 回 revise、科学的誤り 0 件）。原因は計画の要件が合否判定できない粒度だったこと。
- 決定（ユーザー）：Planner の出力に DoD を含める（別の役は置かない）。DoD 外の欠陥は影響する項目・不変条件を示せる場合のみ revise（案 b）。DoD は人の承認対象外。全タスクに適用。dod_infeasible は根拠必須・再計画 1 回まで・却下または再申告で人の判断・研究再試行の回数に数える。
- 仕様：`docs/phase_specs/phase2.md` §14、`docs/phase_specs/phase3.md` §11。
- 実装：`planning/dod.py`（決定論的検証・基本項目 B1〜B5）、`models.DoneCriterion` / `ExecutionPlan.definition_of_done` / `AcceptanceMapping.done_criteria`、計画ループでの検証と seed 再計画、Planner / Plan Reviewer / Implementer / Code Reviewer のプロンプト、`implementation/facts.py`（環境の事実）、`reviewer.judge_against_dod`、`orchestrator._infeasibility_claim`、engine の `_replan_for_dod`。
- 検証：`tests/test_definition_of_done.py`（12 件）ほか全テスト（結果は commit メッセージ参照）。実モデル（Planner が DoD を出力し、Plan Reviewer がそれを審査する）での動作は未検証。

### 監査と修正（2026-10-12 / Claude Code、ユーザー委任）

- UX 方針：`docs/phase_specs/ux_policy.md`（ユーザー決定：errors はボタンなしの調査用、仕様編集で自動再計画、Discord に確認付き Cancel、全 Phase に適用、Run Autopilot は残す、再開までの手順をメッセージに書く）。
- 監査 3 本（今日のコード 14 件、通知 UX 20 件、Phase 10 状態遷移 18 件）→ 修正：`faf133a`（Phase 3）、worktree A（`6b28495`〜`b4d6427`：DoD 引用照合の正規化と DoD 差し戻しの別枠、数値の厳密照合・予算の項目別引用・自律上限の語句対応、Phase 4 環境契約の明記、環境失敗と設定不備の分離）、worktree B（`047cd68`〜`5075e21`：スコープ変更の差し戻し・却下・失効、承認ごとのサイクル上限、完了処理の再試行、tick の例外分離、dedupe、古い制御の無視、予算 None、停止後のスケジュール抑止、Run Autopilot の既存 run 採用、仕様編集での自動再計画、needs_input / 停止メッセージに Resume/Cancel と手順、Cancel の適用と Linear Canceled）、worktree C（`86fb5eb` 相当〜：approvals への集約、手順テンプレート `notifications/guidance.py`、承認依頼の版管理、失効メッセージの編集、制御ボタンの無効化、Phase 5/6B/9 の行き止まり解消、確認付き Cancel ボタン）、`a3c2cb7` / `e29a54c`（Cancel 時に保留中の承認依頼を無効化）。
- 検証：`uv run pytest` 647 passed / 1 warning（既存 discord/audioop）。すべて fake での検証で、実 Discord / Linear / Notion では未検証。
- 未対応（判断待ち・既知）：初期研究計画の Reject は今も研究を CANCELLED にする（方針「打ち切りは明示的な Cancel のみ」と合うか要確認）。Cancel 確認ボタンは 5 分で失効し gateway 再起動で復元しない。DAG 承認の失効後は Run Autopilot で再開（自動再推論なし）。`request_license_decision` は決定済み依頼を返す。単独の Phase 5 run の block には Resume/Cancel ボタンなし（手順のみ）。未回答の承認は約 7 日ごとに再依頼される。Cancel は投入済みの計算ジョブを止めない。

### LLM routing (2026-10-12)

- ユーザー決定（本番まで）：サブスク優先・API は不可能なときだけ。`RA_LLM_SUBSCRIPTION_FIRST=codex`（既定 off）で、`planning/agents.py` `_AgentsSdkBase._run` を通る全構造化呼び出し（Planner / Reviewer、Analyst、Scientific / Completion Reviewer、Dependency Planner、Adapter 生成、`PricedLlm` 経由の文献・Phase 10・外部 repo）が先に `llm_subscription.codex_structured`（`codex exec --json --ephemeral --ignore-user-config -s read-only --output-schema`、空の一時ディレクトリ、API キーなしの sanitized env、effort medium）を試し、usage limit / 認証 / プロセス異常 / timeout / 不正出力（1 回再試行）で従来の API 経路へ fallback。usage limit・認証・codex 不在はプロセス内で 15 分 codex を飛ばす。サブスク呼び出しは cost 0 USD（`billing="subscription"`、`LlmCall` / `LlmUsage` に記録）、API は従来どおり価格計算。価格未設定のモデルはサブスクでは実行し、API への fallback だけを `LlmBudgetError` で拒否。テストは fake codex（`tests/test_llm_subscription.py`）のみで、実 codex での確認は未実施（モデル ID がサブスクで使えるかも未確認、使えなければ毎回 process_error → API）。

### Exact Next Actions

0a. **（2026-10-13）Watchdog（`research-agent autopilot watch`）を追加**（ユーザー依頼）。停止・停滞を Discord #report（`RA_DISCORD_REPORT_CHANNEL_ID=1559474403400491088`、`.env` に設定済み）に 1 回ずつ投稿し、error にはサブスクで読み取り専用の見立てをスレッドに付ける。詳細は `docs/operations/monitoring.md`「Watchdog」。#report への初回投稿（baseline、既存の停止 1 件＝旧 INU-292 は通知しない）と、実データでの見立て 1 回（Codex、印字のみ）を確認済み。**メンションでの修正も導入**（ユーザー承認、manual mode で組み込み）：認可ユーザーが bot をメンション → 専用 worktree / `autofix/*` ブランチで修正・テスト・commit、「適用」で作業ブランチに取り込み（main / push はしない）。偽エージェントでのテストのみで、実エージェントでの修正は未検証。研究室サーバーでの常駐方法（ホスト cron か compose か）は未決定。
0. **（2026-10-12 20:1x JST）Phase A 相当 E2E（INU-324）の到達点と本番投入の判断材料**。
   - **2026-10-13 03:25 JST：親 INU-324 は Done（E2E 完了）**。A1〜A4 すべて Completed。A3 は 1e-10 Eh 許容への修正（ユーザー承認）後に完了、A4 は units チェック修正 `a8a4f0f` 後に完了。子・親の completion review は CLI で代行承認（科学的結論は人の検証前、Phase B 方針は未承認）。Discord general に最終報告済み。ポーリングループ（`poll_loop4.sh`）は終了済み（親は runs に出ないため STAGE 判定が効かず 12 時間で終了した。次回は parent_runs を見ること）。Discord gateway（`discord run`）は稼働中。結果の要約と修正一覧は `docs/operations/phase6b_parent_dag.md`。本番前に人が決めること：本番 Allowed files / Input artifacts、評価規則と tie tolerance（A4 は 1e-12 を使ったが出典未確認）、本番 INU-274〜278 の Linear blocks（現在なし、付けるなら全 edge を明示）、Eagle での Entropy/NOON 参照計算、merge / push。詳細と修正一覧は `docs/operations/phase6b_parent_dag.md`「Phase A 相当の E2E」。
   - **A1（INU-325）・A2（INU-326）は完了**。
   - **A3（INU-327）は科学的な理由で停止（正しい停止）**：Implementer が DoD を実現不能と報告し、1 回の再計画後も同じ報告。理由は 2 点。(1) Oracle Greedy / Beam は空集合から部分集合を育てながら評価するが、承認済みの catalog（81 条件）には k−1 / k の最大部分集合の評価しかなく、途中の energy や greedy の query 数がない。Phase 4D はそれらを承認外の cache で新規 CASCI として計算していた。(2) Entropy / NOON Top-k は全候補空間の FCI/DMRG 参照が必要で、catalog にない。N2 では 7300 万〜1.35 億 determinant で、計画の 4 GB 上限を超える。研究者の判断が必要：新規 CASCI 計算を認めるか（計算量・予算）、Greedy/Beam/Entropy/NOON の扱いを変えるか（仕様の変更）。RA 側では決めていない。
   - **A3 のユーザー判断（Discord、2026-10-12 22:2x）**：新規 CASCI 計算を許可（承認済み cache と同条件、既存 cache は read-only、出力は `outputs/phase_a/` 配下）。Entropy / NOON Top-k はこの E2E では保留（全候補空間の参照は Phase 6.5D 実績でピーク 8–11 GB、Mac の Docker 上限 8 GB に載らない。既存 6.5D DMRG は energy のみで占有なし）。本番では Eagle で参照を計算してから入れる想定。INU-327 コピーに「A3 の計算範囲（E2E 補完）」節として追記し、22:23 に親 INU-324 を Run Requested で再実行。
   - **2026-10-13 01:4x 時点**：A4 にも新規 CASCI 許可（ユーザー、INU-328 コピーに追記）。Linear の E2E コピーに依存を明示（INU-325/326→327、INU-326/327→328。Linear に依存が1本でもあると推論しない仕様のため全部明示）。仕組み修正：`5969ae8`（container mode で host の memory reserve を Docker VM に二重に課さない／初回は推定が上限内なら上限を割り当てる）、`18f4f3d`（再実行時に adopt した完了 child の成果物を schedule 前に capture／upstream 入力に file_sha256）。A3 は実験まで進んだが、A3 のコードの入力チェック（BeO r1.3 で k ごとの候補軌道 digest 不一致＝軌道エネルギー 1e-14 Eh の浮動小数点差）が 'geometry' を含むため triage で numerical_failure→人判断。ユーザーに「軌道エネルギー 1e-10 Eh 許容で一致判定に直して再実行」の可否を Discord で確認中。注意：DAG 承認前に edge を必ず確認する（v16 で A3→A4 が推論から落ちたのを見落として承認した）。
   - **A4（INU-328）は A3 待ち**（DAG v15 で A3→A4）。親 INU-324 は Blocked。approvals に手順付きで通知済み。
   - **本番投入までに人が決めること**：(a) 上の A3 の扱い。(b) 本番 Phase A（INU-274〜278）の Allowed files / Input artifacts（E2E では `src/active_space_selection/phase_a/**`・`scripts/phase_a/**`・`tests/phase_a/**` と catalog 81 条件を仮に使った）。(c) A2 / A4 の tie tolerance・optimum 定義（E2E では 1e-9 Hartree）、A1 の評価規則（optimum hit・1 kcal/mol・成功 0 件の扱い）。E2E コピーの「E2E 補完」節が叩き台になる。(d) main への merge / push（未実施。ブランチ `phase10/research-planning`）。
   - **運用の前提**：`.env` の `RA_LLM_SUBSCRIPTION_FIRST=codex,claude`（Codex → Claude サブスク → API）。トークン上限は cache read を除いて数える（ユーザー決定）。Claude サブスクの費用は API 予算に計上せず、1 回あたりの上限もかけない（ユーザー確認済み）。Discord gateway はコード更新後に再起動すること。poll ループは scratchpad の `poll_loop4.sh`（INU-324、最長 12 時間、16:2x 開始）。
   - テスト：`uv run pytest` 691 passed / 1 warning（既存 discord/audioop）。

0. **（2026-10-12 12:4x JST）Phase A 相当の E2E（INU-324）を実行中**。サブスク優先（`RA_LLM_SUBSCRIPTION_FIRST=codex`）を実機確認のうえ `.env` で有効化（gpt-5.6-terra / sol とも codex exec で構造化出力 OK）。見つけた仕組みの不具合と修正：`6fc6cc8`（DAG 推定の根拠がラベル付き引用で弾かれる → 引用部分の照合、言い換えは除外）、`e1aeb09`（推定の検証失敗を「仕様を直せ」と案内しない）、`9fc8acd`（兄弟が動いている間に子が止まっても approvals に即通知）、`2853508`（catalog の content_hash を `sha256=` と表示していたため実装者がファイル SHA-256 と比べて DoD 不能と判断 → `file_sha256` を manifest に追加し表示を修正）、`98b48ce`（止まった親を Run Autopilot で再実行すると、止まった子が Linear In Progress のままで intake が拒否 → 止まった子を Todo に戻す）。E2E コピーの仕様補完（ユーザー許可「足りない情報は適当に補っていい」）：INU-325 に評価規則（optimum hit、1 kcal/mol、成功 0 件の扱い）、INU-326 に整合確認の代替（Phase 4 には fingerprint がない、D-067）、子 4 件の Allowed files 注記を「E2E で承認済み」に修正（旧注記「本番では未承認」を Reviewer が未承認と解釈して停止）。追加の修正（12:4x 以降）：`55e558b`（container 内でワークツリーの .git が見えず commit 由来のテストが落ちる → 主 repo の .git を ro マウント、Claude fallback が repo のテストコマンドを実行可能に）、`e363a16` / `039d0fa`（Claude サブスクの名目コストを API 予算に計上しない、サブスク時は 1 回あたりの上限をかけない）、`8740124` / `dbddbb6`（上限内だが今の空きメモリが足りないジョブは最大 30 分待つ、`RA_EXP_RESOURCE_WAIT_MIN`）、`d267f02`（container provenance の相対パス二重化）、`75367fb` / `9a40d2d`（止まった親の再実行で子を継続：Phase 3 の続き、実験だけ止まった子は Phase 4 を 1 回だけ再実行）、`db946af`（DoD の引用：複数箇所の引用を空行区切りで許可、和文と英字の間の空白の揺れを無視、失敗メッセージに見つからない箇所そのものを出す）、`73041b1`（構造化呼び出しを Codex → Claude サブスク → API の順に。`.env` は `RA_LLM_SUBSCRIPTION_FIRST=codex,claude`。実機確認済み）、`cded2a1`（container モードで runner が設定する RA_CATALOG_INPUTS をホストに要求して dead letter）、`42dbea4`（出力内の units 辞書を値として読んで単位検査が失敗）。Codex サブスクは 13:15 回復後 14:2x に再び上限（回復 18:18）：構造化呼び出しもサブスクにしたため消費が速い。**運用上の注意**：Discord gateway（`discord run`）は起動時のコードで動く。2026-10-11 21:09 起動の gateway が UX 修正（02:35）後も旧コードのまま動いていたため 12:4x に再起動した。コード更新後は gateway も再起動すること（本番は compose の再起動）。

0. **（2026-10-12 06:2x JST）OpenAI API のクレジット切れで LLM 段階がすべて停止中**（`credit_balance_exhausted`）。ユーザーに Discord 一般チャンネル（1558257920125308991）で補充を依頼済み。補充されるまで：INU-318 は Stop で一時停止（サイクル 2 の最終集約 INU-323 が codex_api の quota で止まった状態。補充後に `research resume INU-318` → サイクル内再試行）。Codex サブスクは 08:14 に回復。
0. **Phase A 相当の E2E（ユーザー指示：08:15 以降に開始）**：Notion/Linear に `[E2E]` コピーを作成済み（親 INU-324、子 A1 INU-325 / A2 INU-326 / A3 INU-327 / A4 INU-328、元 INU-274〜278）。子には E2E 用の仮設定として Allowed files（`src/active_space_selection/phase_a/**`、`scripts/phase_a/**`、`tests/phase_a/**`）と Input artifacts（catalog 81 条件）を追記（本番 Phase A では未承認）。DAG dry-run は valid（依存は Dependency Planner が提案 → dev CLI で承認）。開始方法：INU-324 の Notion Run Requested を ON（または Run Autopilot）。承認は `research-agent research approve <task> --actor ...`（dev 限定）。
0. Phase A 向けの準備（済）：local DB に Catalog Adapter `active-space-phase4-orbital@1.0.0` を登録・有効化（actor は claude-code、ユーザー委任）、scan で 81 件 verified。`config/research_repos.toml`（Git 管理外）の active-space-research に `[container]`（compose_project active-space-ra / service research / workdir /workspace）を追加 → Phase 4 は研究 repo の Docker イメージで実行（`d60f58d`）。`.env`（Git 管理外）：`RA_ENVIRONMENT=dev`、`RA_IMPL_FALLBACK_BACKEND=codex_api`、`RA_IMPL_WALL_TIME_SECONDS=3600`。
0. **（2026-10-12 05:3x JST）INU-305 の E2E は完了**（受け入れ条件 1〜6 の確認は `docs/operations/phase10_research_orchestration.md`「INU-305 の E2E 結果」）。**INU-318（2 回目、同一仕様）を実行中**。ユーザー委任（2026-10-12）：承認・Resume は代行可（dev 限定 CLI `research-agent research approve` / `research resume`）、仕組みの改修は自律的に行ってよい（commit を打つ）、INU-318 が長引く場合は E2E をもう 1 回まで実施してよい。poll ループ：scratchpad の `poll_loop2.sh`（INU-305/318 が done で終了、最長 10 時間）。
0. **（2026-10-12 監査後・済）INU-305 の E2E を再開する**：`research resume INU-305`（またはユーザーの Run Autopilot）→ 研究スコープの承認依頼が approvals に届く（予算 API $10 が確定しているか確認）→ ユーザー承認 → 監視。
0. **（2026-10-12 01:4x JST、ユーザー決定）進め方：① UX 方針の議論・確定 → ② 監査（不具合、UX 方針に沿わない部分、次の段階へ進めない行き止まり）→ 修正 → ③ E2E を最初から実施。** INU-292 は打ち切り・キャンセル済み（INU-292〜304 を Linear Canceled、DB の WorkflowRun は cancelled）。新しい E2E タスクは **INU-305**（Notion https://app.notion.com/p/E2E-Phase-10-H-RHF-FCI-2026-10-12-3f669893cf8f81eaa1e7c0daa2af091c、仕様は INU-292 と同一＋予算行に「API 費用 10 USD」をユーザーが追記）。INU-305 は計画 v3 が needs_input で blocked（原因の数値照合バグは `d483b91` で修正済み、未再開）。ポーリングは停止済み、Discord gateway（PID 18313）は稼働中。監査後に INU-305 を Run Autopilot（または `research resume INU-305`）で再開する。
0. 監査で必ず扱う既知の UX / 仕組みの課題：(a) needs_input の通知は errors に Resume なしで届き、Discord からは再開できない（設計上の再開手段は Notion の Run Autopilot。テスト `test_resume_on_a_blocked_research_run_restarts_planning` が「ボタンなし」を固定している）。(b) 予算 `BudgetItem` は引用を1つしか持てず、CPU と API の予算が別の行だと ambiguous になる。(c) Autopilot は Linear を Canceled にできない（人の操作扱い）ため、打ち切りの手順が CLI にない。(d) DoD・codex_api fallback・継続再開（b69f8bd）は実データ未検証。
0. **DoD の実データ確認**：INU-292 の次の再試行で、Planner が引用付きの DoD を出し、決定論的検証を通り、Code Reviewer が項目別に判定することを確認する。DoD の決定論的検証で Planner が収束しない（`plan not approved after N replans`）場合は、引用一致の厳しさを見直す。
0. **（2026-10-12 00:3x JST 時点）INU-292 サイクル 5 の再試行を実行中**：INU-303 の Phase 3 が API 課金 Codex（`codex_api`）で実装中。経緯と 2026-10-12 の修正（`12807f6` `542e590` `75daadc` `b69f8bd`）は `docs/operations/phase10_research_orchestration.md`「2026-10-12 の仕組み修正」。ポーリング loop（前セッションの scratchpad の `poll_loop.sh`、PID 18811）は 01:10 JST ごろ 4 時間上限で終了するため、終了を `ps -eo pid,etime,command | grep "autopilot poll"` で確認してから同文書の手順で 1 本だけ再起動する。Discord gateway は `uv run research-agent discord run`（PID 18313）が稼働中。
0. Phase 10 E2E（INU-292）の続き：`docs/operations/phase10_research_orchestration.md`「引き継ぎ：E2E の続け方」に従い、ポーリングと Discord gateway が動いているか確認し、サイクル 4 以降を監視する。完了したら受け入れ条件（INU-292 の Acceptance 1〜6）を確認し、結果を同文書に記録してユーザーに報告。main merge / push はユーザー指示待ち。

0. Phase 9 は `phase9/external-repositories` で実装済み。main への merge と push はユーザー指示待ち。
0. Phase 9 未検証：Eagle（Linux）での Docker 隔離と wheel 取得、研究ジョブが外部コード保管場所を絶対パスで読む経路の技術的遮断（現状は Implementer 規則と Reviewer 依存）。

0. Phase 8 は `phase8/literature-research` で実装済み。main への merge と origin への push はユーザー指示待ち（ユーザーは push 前に Phase 8 まで進める方針）。
0. Phase 8 要件照合（2026-10-11）で補った漏れ：公式ドキュメント（HTML）の取得・解析、文献調査費用の Phase 5 予算への計上、Purpose を使った資料選定、検索候補の記録、Planner 依頼の seed 抽出。ブランチ `phase8/gap-fixes`。公式ドキュメントは pyscf.org で取得・本文抽出まで実サイト確認、LLM 抽出を含む E2E は未実施。
0. 購読論文：ユーザー決定（2026-10-11）により CAPTCHA が出ない限り Zotero Connector で自動保存、1 回の調査あたり自動取得 10 件まで（`acquire.MAX_RETRIEVALS`）。実出版社ページでの検証は未実施。
0. 未検証：Eagle（Linux）での bwrap sandbox と catalog scan、本番 PostgreSQL での運用、autopilot poll からの生成自動起動（未実装・仕様外）。
0. 本番 DB で使う場合は人間が `research-agent catalog adapter register/validate/activate/bind/scan` を実行（`RA_CATALOG_ENVIRONMENT=mac`）。Phase A の A2/A3 で cache を使うなら、人間が Input artifacts に `catalog: kind=orbital-casci-exhaustive-condition repository=active-space-research molecule=N2,BeO,NH3 k=2..10 git_commit=6c0d1dd expect=81` を追記するか判断する（科学仕様の変更にあたるため RA は書かない）。
1. Phase A の本番投入はユーザー方針により**全 Phase 完了後**。それまで Phase A の実行・E2E はしない。
2. 投入前に人間が INU-275〜278 の `Allowed files` を決めて Notion に `## Allowed files` 見出しで記入する（候補：`src/active_space_selection/`、`scripts/`、`tests/`。ページ上は未承認）。A1〜A4 の Linear blocked-by は任意（未設定なら Dependency Planner が提案し Discord 承認）。
3. 記入後に read-only dry-run（`discover_and_build(..., registered_repositories=set(deps.impl.registry))`）で DAG が valid / problems 0 を確認してから Run Autopilot を押す。詳細は `docs/operations/phase6b_parent_dag.md`「Phase A 準備状況」。

### Phase 5 Discord 通知・承認 UI（2026-10-10 / Codex）

- `RA_NOTIFICATION_BACKEND=discord` と `RA_APPROVAL_MODE=discord` を既定にし、`.env.example` に Bot token、guild、承認者 user ID、approvals / results / errors / logs の4チャンネル ID を追加。ntfy と旧 Linear / Prefect form モードは移行互換として残した。
- 型付きイベントを4チャンネルへ決定論的に routing し、Discord Embed を生成する `notifications/discord.py` を追加。通知は `autopilot_notifications` に pending / failed / sent、attempt、next retry、message ID を永続化する。
- Gateway consumer を `research-agent discord run` と Compose の独立 `discord-bot` service として追加。`discord.py` 2.7.1 を使用し、特権 Intent は使わない。ライブラリの rate-limit / reconnect と、アプリ側の指数 backoff、DB lease、Embed footer marker による crash-window の重複回避を組み合わせた。
- approval message に Approve / Reject / Request Revision の persistent button と revision modal を追加。guild、approvals channel、user allowlist、bot user、pending、expiry、Interaction ID / request ID の重複を検証し、入力だけを DB に保存する。
- Approval Controller が polling 時に current Notion spec hash を再検証して初めて決定を確定する。Linear Status 変更は承認証拠に使わない。確定後は approve を実行状態へ、reject を Canceled、revise を Revision Requested へ反映し、Discord の元ボタンを無効化する。
- task start、phase transition、retry、proposal、experiment、analysis、task completion、budget、unrecoverable error、repair success を typed event として追加。通知失敗は通常処理の成否を変えず、承認通知が失敗しても自動承認しない。
- Discord Bot token を coding agent / reviewer / check command の環境から除外するよう `sanitized_env` を拡張。
- `research-agent discord check` で実 Bot の Gateway 接続、guild、4チャンネルの view/send/embed/history 権限を確認。`--send-test` で各チャンネルに「実タスクではない」と明記した Embed を1件ずつ投稿した。実ユーザーによるボタンクリックは未確認。
- 関連テスト94件が成功（Discord 27、既存通知・Phase 3/4 flow 21、Phase 5 46）。ユーザー指示により全 pytest は実行していない。

### Phase 5 結果登録 UX 改善（2026-10-10 / Codex）

- Research Notes 作成時に、ステータスを「採用」、分類（multi-select）を「結果」に設定するよう変更。
- Autopilot が追加提案の Linear Issue を作成するとき、Linear API の `viewer` を担当者に設定するよう変更。
- コネクタテストを2件追加。関連テスト 58 件、全テスト 317 件が成功。
- 結果 Notes を「結論 / 計算結果（表）/ 検証状況 / 注意点 / 次のアクション / 折りたたみの参照・再現情報」に変更。数値・単位・判定・参照は structured data から決定論的に生成し、レポート用 LLM 呼び出しは追加していない。
- 詳細 evidence、checksum、job ID、一時絶対パスは本文から除外し、PostgreSQL / ExperimentManifest / JSON artifacts に保持する。主要 artifact は repo-relative path で表示する。
- `OutputFile.units` のフィールド名を Validator が JSON Pointer として扱えなかった不整合を修正。フィールドの存在、数値型、有限値と宣言単位を検証し、metric の宣言単位も passing evidence として記録する。
- Completion Reviewer へ引用可能な passing evidence 識別子を明示し、存在しない・言い換えた evidence は従来どおり最終ゲートで `unverifiable` に落とす。
- INU-279 の実測値を structured fixture にし、表示丸めと元精度の分離、単位、重複排除、判定の維持、監査情報への追跡、一時パス非表示を検証。
- 関連テスト：Notes / Phase 4 / Phase 5 E2E / connectors の81件、Completion / Linear approval の23件が成功。全テストはユーザー指示により再実行していない（直前の小変更前ベースラインは317件成功）。

### Phase 5 実装（2026-10-10 / Claude Code）

- 仕様書は `docs/phase_specs/phase5.md`。
- ユーザーとの合意事項（いずれも 2026-10-10）：
  - 承認：最初は「Linear の Status ＋ Prefect のフォーム」で合意したが、その後 **Linear の Status だけで承認する方式に変更**。承認者は登録したユーザー ID で確認する（後述）。
  - Linear に workflow state（Awaiting Approval / Approved / Revision Requested）を追加。
  - Notion への書き込みはテスト用ページに限る。
  - 予算は API $5 / 計算 4 CPU 時間。
- `src/research_autopilot/autopilot/`：
  - `models`：ResearchAuthorization / ApprovalPolicy / ParameterDomain / ApprovalRequest / ApprovalDecision / ResearchTaskProposal / OutboxItem / WorkflowRun / CompletionReview / CompletionAssessment
  - `policy`：決定論的な Approval Policy Gate
  - `approvals`：承認の検証。linear / linear+form モード
  - `linear_ops`：書き込みのガード（承認用の状態への遷移を禁止、[TEST] 限定モード、自分の書き込みの監査）
  - `outbox`：分散書き込み。マーカーで既存の書き込みを取り込み、dead-letter あり
  - `completion`、`notes`、`budget`
  - `engine`：状態遷移、polling、重複の検出、通知の重複防止
  - `flows`（autopilot-poll / approval-confirm）、`factory`、`settings`
- 共用部分の拡張：`LinearWriter`（コメント、Issue の作成、history）、`NotionWriter`（Tasks / Notes のページ）、`config.load_dotenv`（CLI が `.env` を直接読む）、`NotificationEvent.details`、Phase 5 用の DB テーブル7つ。
- 安全性の修正：Coding Agent / Reviewer / 検証コマンドに渡す環境変数から認証情報を取り除くようにした（`implementation.backends.base.sanitized_env`）。それまでは LINEAR_API_KEY などが Codex / Claude のプロセスに渡っていた。
- CLI：`research-agent autopilot poll|status|approve|cancel|resume`。deployment：`autopilot-poll/scheduled`（inactive で作成）、`approval-confirm/manual`。
- ドキュメント：`docs/operations/phase5_autopilot.md`（承認の検証、設定、上限、復旧、Eagle への一括反映と rollback の手順）。

### Phase 4 実装（2026-10-10 / Claude Code）

- 仕様書は `docs/phase_specs/phase4.md`。
  - 既存の supervisor はなかったので、自前の shim と台帳を作った。
  - LLM はユーザーが設定した単価の範囲で使う。
  - Mac では安全側の上限を使う。
  - 実行承認は Phase 3 のゲートを再利用する。
- `src/research_autopilot/experiments/`：
  - `models.py`：JobSpec / JobHandle / JobStatus / Allocation / ValidationResult / TriageDecision / AnalysisResult / ExperimentProposal / ScientificReviewResult / ScopeAssessment / ReviewedExperimentProposal / ExperimentManifest / Phase5Handoff（すべて schema 1.0）
  - `resources.py`：ホストの検出、見積もり、ResourceManager、スレッド数の設定
  - `environment.py`：git archive による checkout、キャッシュした venv と freeze による来歴
  - `shim.py`：切り離して動く supervisor（heartbeat、RSS の計測、上限の強制、stdout / stderr を分離）
  - `backends/`：local（台帳、冪等な submit、照合）、slurm（sbatch / sacct / squeue / scancel。設定がなければ blocked）、fake
  - `validator.py`、`triage.py`
  - `analyst.py`：Agents SDK。単価が設定されていないモデルは呼ばない
  - `scope.py`、`orchestrator.py`、`flow.py`、`factory.py`
- 共用部分の拡張：
  - `implementation.orchestrator.check_execution_approval`（Phase 3 のゲートを外から使えるようにした）
  - DB に `experiment_runs` / `experiment_jobs` / `experiment_proposals` を追加
- CLI は `research-agent experiment run|resume|list|show|proposals`。deployment は `run-experiment/manual`。設定は `.env.example` の `RA_EXP_*`。
- ドキュメント：`docs/operations/phase4_experiments.md`（Phase 5 handoff の schema、失敗時の振り分け、Eagle への反映手順）。

### Phase 3 実装（2026-10-10 / Claude Code）

- 仕様書は `docs/phase_specs/phase3.md`。§10 は Phase 4 連携の追加仕様。ユーザーと合意した事項：
  - 実行承認：apply モードの handoff ＋ Linear が In Progress
  - リポジトリ：登録ファイル ＋ Notion の Repository / Allowed files
  - 費用：サブスクリプションの CLI は回数・時間・トークン数で制御し、従量課金の API は単価と予算上限が必須
  - Claude の実動作テスト：可
- Phase 2 への後方互換な拡張：見出しに Repository / Allowed files / Non-goals / Inputs を追加。`TaskSpec` と `ExecutionHandoff` に省略可能なフィールドと `phase2_mode` を追加。
- `src/research_autopilot/implementation/`：
  - `orchestrator.py`：承認ゲート、リース、worktree、試行ループ、修正・昇格・fallback、予算、manifest、復旧、`repair()`
  - `backends/`：codex.py（`codex exec --json`、stdin を閉じる）、claude.py（`claude -p --output-format json`、`--max-budget-usd`）、review_runners.py（read-only の独立セッション。`--output-schema` / `--json-schema` を使う）
  - `worktree.py`：ブランチ `autopilot/<task>-<run8>`、ロック、diff と commit、Git 監査ログ、main は保護、生成物は除外
  - `reviewer.py`：scope / 保護パス / 危険な変更 / pytest / lint / typecheck / syntax / experiment_spec の検証と、LLM レビュー（pass / revise / blocked）
  - `classify.py` と `routing.py`：ルールによる難易度・科学的リスクの分類、tier の選択、昇格
  - `experiment.py`：ExperimentDefinition / ExperimentSpec（1.0）と、固定 commit に対する検証
  - `failure.py`：FailureReport（1.0）と修正の可否を決めるポリシー
  - `flow.py`：Prefect flow `implement-task` / `repair-task`
  - `factory.py`
- 永続化：`implementation_runs`（`failure_report_id` は unique、`parent_run_id`）、JSON の manifest / spec / handoff。
- CLI：`research-agent implement`（`--task` / `--handoff` / `--resume`）、`repair <report>`、`impl-runs list|show`。
- 設定：`config/research_repos.example.toml`（実ファイル `config/research_repos.toml` は Git 管理外）、`.env.example` の `RA_IMPL_*` と `RA_*_MODEL_*`、`prefect.yaml` の deployment 2つ。
- ドキュメント：`docs/operations/phase3_implementation.md`（Phase 4 インターフェースの仕様を含む）。

### Phase 2 実装（2026-10-10 / Claude Code）

- 仕様書は `docs/phase_specs/phase2.md`。ユーザーと合意した事項：
  - 承認：Linear Todo かつ Notion Pending=false
  - スコープ：ラベル `autopilot`
  - 依存：Linear の blocked-by
  - モデル：Planner `gpt-5.6-terra`、Reviewer `gpt-5.6-sol`
  - Eagle：Windows + WSL2
- コード（`src/research_autopilot/`）
  - `tasks/`：Notion connector（読み取り専用、API 2025-09-03、見出しを正規の項目に対応づける）、Linear connector（GraphQL、限定的な再試行）、決定論的なゲート、TaskManager（Notion と Linear の対応確認、`--next` の選択）
  - `planning/`：Agents SDK の Planner / Reviewer（Pydantic の strict 出力、エラーを3種類に分類）、再計画ループ（毎回レビュー、最大2回）、コスト推定（単価が分からなければ null）
  - `orchestrator/`：`core.plan_task`（リース、最後のゲート再確認、apply 時だけ Linear を In Progress にする。started 以外の state へは更新しない）、Prefect flow `plan-task`（markdown artifact を作る）
  - `storage/`：SQLAlchemy の `plan_runs` / `task_leases` / `linear_status_audit`、JSON 記録、Phase 3 用の handoff JSON
  - `cli.py`：`research-agent plan|runs|db`
  - `models.py`：TaskSpec、ExecutionPlan、ReviewResult、WorkflowDecision、BlockedReason など
- インフラ：worker に Phase 2 の環境変数と `RA_DATABASE_URL`（同じ PostgreSQL 上の別 DB `research_autopilot`）を追加。`prefect.yaml` に deployment `plan-task/manual` を追加（既定は dry_run）。
- ドキュメント：`docs/operations/phase2_planning.md`、README、AGENTS、docs/README。
- 依存の追加：`openai-agents`、`sqlalchemy`、`psycopg[binary]`。

### Phase 2 事前設定（2026-10-10 / Claude Code + ユーザー）

- 詳細と結果は `docs/operations/phase2_prerequisites.md`。
- `.env.example` に `NOTION_API_TOKEN` / `NOTION_TASKS_DATA_SOURCE_ID` / `LINEAR_API_KEY` / `LINEAR_PROJECT_ID` / `OPENAI_API_KEY` を追加した。秘密でない ID 2つは確認済みの値を記入。
- ユーザーが Mac の `.env` に秘密の値を設定した。`LINEAR_PROJECT_ID` はユーザーの承認を得て、正式な UUID に置き換えた。
- 接続確認：Notion PASS、Linear 読み取り PASS、Linear 書き込み PASS（テスト用 issue INU-270 だけを変更し、元に戻した）、OpenAI PASS（クレジット追加後）。
- Phase 2 の仕様（Notion ページ、INU-265）を API で読めることを確認した。`docs/phase_specs/phase2.md` はまだ作っていない（Phase 2 の実装依頼時に作る）。
- 新しい機能、依存、Compose の変更はしていない。worker も再起動していない。

### Phase 1 実装（2026-10-10 / Claude Code）

- `docs/phase_specs/phase1.md` を作成した（ユーザー指示「phase1 の要件は claude 側で作成」による）。ユーザー提示の Phase 1 指示 §1, §3〜§8, §10 を整理したもので、要件の追加や変更はしていない。
  - なお `docs/phase1_technical_design.md` §1 の「phase1.md は存在しない」という記述は Codex の文書なので書き換えていない。現在はこのファイルが存在する。
- Python（`src/research_autopilot/`）
  - `flows/smoke_test.py`：次の5つの flow。各 flow に completed / failed / crashed の通知 hook を付けた。
    - `smoke-success`：冪等な副作用付き
    - `smoke-failure`：`TransientError` に限り最大2回 retry（指数 backoff + jitter）
    - `smoke-timeout`：task timeout 2秒
    - `approval-demo`：通知してから `pause_flow_run`
    - `smoke-sleep`：復旧訓練用
  - `idempotency.py`：`apply_once`。tmp ファイルを書いてから `os.link` でアトミックに作成し、payload が食い違えば検出する。
  - `notifications/`：型付きの `NotificationEvent`（例外メッセージは入れず型名だけ）、ntfy 互換の `HttpNotifier`（timeout 5秒、5xx と通信エラーのみ最大2回 retry）、`LoggingNotifier`（未設定時）、`safe_send`（例外を外に出さない）、Prefect hook。
  - 依存に `httpx>=0.28.1` を追加（直接 import しているため）。
- インフラ
  - `infra/compose.yaml`
    - `postgres:16.15`：named volume、host にポートを出さない
    - `prefecthq/prefect:3.8.8-python3.12` の単一プロセス server：`127.0.0.1:4200` のみ、Basic auth、CSRF 有効
    - process worker：`research-local` pool、`--limit 1`、`--with-healthcheck`、非 root、docker.sock なし
    - 共通：healthcheck、`depends_on: service_healthy`、`restart: unless-stopped`、ログのローテーション
  - `infra/Dockerfile`：uv 0.12.18 を使い、`uv sync --frozen --no-dev` でコードを `/opt/research-autopilot` に焼き込む。uid 10001 で実行する。
  - `infra/automations/crash-zombie-flow-runs.yaml`：heartbeat が 540 秒途絶えた run を Crashed にする。
  - `prefect.yaml`：5つの deployment。version は `GIT_SHA`。スケジュールは inactive で登録する。
  - `.env.example`、`.dockerignore` を追加。
- テスト（`tests/`、25件）：冪等性（逐次、16スレッド同時、payload の食い違い、不正な ID）、通知（payload、token の秘匿、retry、4xx を再送しない、失敗の隔離、設定の validation）、flow（成功、重複実行、retry 後の失敗、retry での回復、timeout、通知失敗が state に影響しないこと、sleep）。
- ドキュメント：`README.md`、`docs/operations/{deployment,monitoring,recovery}.md`。`docs/README.md` と `AGENTS.md` の構成図を更新した。

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

なし（ユーザーレビュー待ち）。

## Remaining Work

- **Phase A source-data blocker**（2026-10-11 更新）：Acceptance Criteria / label / Specification / Repository / Registry は解消済み。残りは INU-275〜278 の `Allowed files`（人間の判断待ち）のみ。Linear blocked-by は任意。
- **Eagle への一括反映と TSUBAME**（Phase 5 の後にまとめて行う方針）：`phase5_autopilot.md` §7 の手順。worker / discord-bot image、DB の新規 Interaction table、Bot token と4チャンネル、Codex / Claude CLI の認証、volume、上限と予約を確認する。
- 実 Discord でユーザーが承認ボタン / revision modal を操作し、polling が再開して Linear と元メッセージを更新する一連の確認（Fake E2E では確認済み）。本番タスクを作成・変更せずに止めている。
- 実データでは確認できていないこと：依存関係のある複数タスク、追加の提案が自動で登録されて実行される流れ（実際の Analyst が提案を0件にしたため）。どちらも Fake の E2E では確認済み。
- 既存の INU-279 Notes は旧形式のままで、今回の Human Report 形式への backfill と実 LLM Completion Reviewer での再判定は未実施（本番 Notes を無断更新しない方針）。新規 run では passing evidence の正確な識別子を Reviewer に提示する。
- テストデータ：INU-279（Done）、INU-271〜273、INU-270、Notion のテストページ、Notes「[AP] [TEST] …」、ラベル `autopilot` を残すかどうか（ユーザー判断）。
- Mac の検証用環境：PostgreSQL を `127.0.0.1:5433` で公開する override（scratchpad 内）で再起動したままになっている。不要なら override を外して `up -d postgres` する。

## Changed Files

- Discord 移行（`d5e3138`）：新規 `notifications/discord.py`、`tests/test_discord_notifications.py`。変更 `.env.example`、`infra/compose.yaml`、`pyproject.toml` / `uv.lock`、notification / approval / engine / CLI / DB / flow / secret sanitization。
- Discord 運用文書：`README.md`、`docs/operations/{deployment,monitoring,phase5_autopilot}.md`、本ファイル。

- 新規：`docs/phase_specs/phase1.md`, `src/research_autopilot/idempotency.py`, `src/research_autopilot/flows/smoke_test.py`, `src/research_autopilot/notifications/{__init__,events,http,hooks}.py`, `tests/{conftest,test_idempotency,test_notifications,test_smoke_flows}.py`, `infra/{compose.yaml,Dockerfile}`, `infra/automations/crash-zombie-flow-runs.yaml`, `prefect.yaml`, `.env.example`, `.dockerignore`, `docs/operations/{deployment,monitoring,recovery}.md`
- 変更：`pyproject.toml`, `uv.lock`（httpx を追加）、`README.md`、`AGENTS.md`（構成図）、`docs/README.md`（構成図・役割表）、本ファイル

## Tests and Verification

### Discord 通知・承認 UI（2026-10-10、MacBook）

- `uv run pytest -q tests/test_discord_notifications.py`：27 passed。routing、Embed、厳格な設定、Gateway fail-closed、outbox の dedupe / 再起動復元 / retry、権限外・bot・別 guild / channel・期限切れ・処理済み・二重 Interaction、Approve / Reject / Revision、spec stale、Linear 反映、token 除外。
- 既存通知・Phase 3/4 flow：21件、100% passed。completed / failed hook、repair / experiment / analysis event を確認。
- Phase 5 関連：46 passed。旧 linear mode の後方互換、policy / outbox / engine / Fake E2E / Prefect flow / CLI を確認。
- `docker compose --env-file .env -f infra/compose.yaml config --quiet`：成功。`research-agent discord --help` / `discord check --help`：成功。
- 実 Discord：`research-agent discord check` と `--send-test` が成功。Bot token は出力せず、4チャンネルの権限と各1件のテスト Embed を確認。
- 全 pytest はユーザー指示（変更部分に関連するテストのみ）に従い、今回の Discord commit 後には実行していない。

### Phase 5（2026-10-10、MacBook）

**自動テスト**：`uv run pytest` で **315 passed**（Phase 1〜4 は 269 件、Phase 5 は 46 件）。Phase 5 のテストは Fake の Notion / Linear（API key の actor がユーザーと同じになる、実際の挙動を再現）と Fake の Planner / Implementer / Runner / Analyst を使い、Phase 2 / 3 / 4 は実際の orchestrator を通した。

Fake の E2E で確認したこと：

- 初回の委任承認のあと、途中の承認0回で Done まで完走し、範囲内の追加実験が自動で登録・実行された（inherited authorization、depth 1）。
- 次のケースは、承認待ちで止まり、正しい人間の操作でだけ再開した：範囲外の提案、invariants の変更、acceptance の緩和、上限の超過、予算の超過、判定できない場合。
- 次の操作では再開しなかった：bot による変更、権限のない actor、Autopilot 自身の書き込み、要求より前の遷移、仕様を編集したあとの古い承認、フォームと Linear の不一致。
- Notion は成功して Linear が失敗した場合の照合（重複なし）、Notes の書き込みに失敗したときに Done にならないこと、検証の失敗や根拠の捏造で Done にならないこと、仮説が否定されても成功として扱うこと、提案の上限、似た提案は人間の確認に回すこと、polling の排他、クラッシュ後の再開、dead-letter、dry-run、test-only モード。

**実データでの E2E**（Mac。実際の Notion / Linear はテスト用タスク INU-279 のみ。実際の Phase 2 LLM、Codex、LocalBackend 上の PySCF、Analyst / Completion Reviewer、Prefect）：

| 段階 | 結果 |
|---|---|
| 発見と初回の委任承認 | `autopilot poll` で発見 → Linear を Awaiting Approval に（Autopilot が書き込み）→ ユーザーが Approved に → **Linear の Status 変更だけで検証を通過**（actor id `ead20c4c…` が登録した承認者と一致、非 bot、要求の後、自分の書き込みではない） |
| Phase 2 | 実際の Planner / Reviewer が計画を承認し、Linear を In Progress に |
| Phase 3 | 実際の Codex（gpt-5.6-sol、2回の試行）、Code Reviewer が pass、commit `18cd7d6` |
| Phase 4 | 実際の PySCF で検証に合格（RHF −1.11676 / FCI −1.13728 / 相関 −0.02052 Ha）。Analyst は提案0件。LLM の費用 $0.023 |
| 完了 | Completion Reviewer（$0.010）→ 科学的リスクが high のため人間の確認を求めた（基準1は unverifiable、基準2は met）→ ユーザーが Approved に → Notes に書き込み、Linear にコメント、Done |
| 結果の反映 | Linear の INU-279 は Done で、コメントに Notes の URL、run、commit、基準の判定。Notion Notes に、Summary から Provenance までの10見出しのページ |
| 承認の回数 | 2回（初回の委任と、high リスクのための完了確認）。途中で追加の承認はなし |
| Prefect の承認フォーム（参考） | ローカル Compose の worker 上で、suspend（worker を占有しない）→ API で resume → PostgreSQL に記録、まで実際に確認した。UI v2 では Resume の場所が見つからなかったため、既定のモードを linear に変更した |

**実データで確認していないこと**：依存関係のある複数タスク、追加の提案の自動登録と再実行（Fake では確認済み）、ntfy の実際の送信、Eagle と TSUBAME。

### Phase 5 Acceptance（指示 §9）

| 項目 | 判定 |
|---|---|
| 登録 → polling → Phase 2 → 3 → 4 → Notes → Linear Done の E2E | Passed（実データ：INU-279。Fake も） |
| 承認不要の実装・実験で途中の承認が0回、範囲内の追加実験の自動登録と再実行 | Passed（Fake）。実データでは途中の承認0回（high リスクの完了確認を除く）。追加実験は提案が出なかったため未確認 |
| 範囲外・invariants・評価基準・費用・判定不能で Awaiting Approval に止まり、正しい人間の操作だけで再開 | Passed（Fake ＋ 実データの承認2回） |
| bot / 古い承認 / 偽の操作では再開しない | Passed（Fake） |
| 部分的な成功、重複登録、重複通知、polling の競合、依存の順序、上限、予算、worker の再起動、API の障害、長時間の実験からの復旧、Phase 3 での修正の上限 | Passed（Fake ＋ Phase 4 の実データテストで確認済みの復旧機構） |
| 成果物の不足、科学的な検証の失敗、Reviewer の未確定、Notes の書き込み失敗で Done にしない | Passed（Fake） |
| 仮説を否定する結果を成功として扱う | Passed（Fake） |
| Fake と実データの区別。Eagle 未デプロイ、TSUBAME 未接続 | 上の表のとおり（どちらも PASS としていない） |
| Phase 1〜4 の regression | Passed（315 件） |

### Phase 4（2026-10-10、MacBook）

**自動テスト**：`uv run pytest` で **269 passed**（Phase 1〜3 の 218 件と Phase 4 の 51 件）。Phase 4 のテストは FakeComputeBackend / Fake Analyst / Fake Reviewer を使い、Phase 3 の run（fakes）から作った実際の ExperimentSpec で実行した。LocalBackend のテストは**本物のプロセス**で行った：submit / status / cancel / collect、メモリと wall time の上限の強制、重複 submit、起動前のクラッシュ（never_started → 再投入）、pid 保存前のクラッシュ（引き取り）、shim の強制終了（LOST）、秘密値がディスクに残らないこと。Slurm は fake executor で確認した。

**実環境の統合テスト**（scratchpad の `ra-h2-research`）：

- Linear / Notion は変更していない。Phase 2 の承認は fakes で用意した。
- 実験のバリエーションは、実際の Phase 3 orchestrator に台本どおりに書く実装者を組み合わせて作った。
- LocalBackend、shim、venv（PySCF）、validator、Phase 3 での修正（実際の Codex）は本物を使った。

| テスト | 結果 |
|---|---|
| 並列実行と依存順序 | h2-a と h2-b が同時刻に実行（21:20:48 → 21:21:12）、両方の完了後に h2-combine（21:21:14〜）。3件とも検証に合格し、RHF −1.11676 / FCI −1.13728 / 相関 −0.02052 Ha。venv は1回だけ作られ、成果物は 444 と sha256 つきで保存 |
| わざと失敗させた実験 → Phase 3 での修正 → 再実行 | exit 0 なのに `results/h2.json` がないことを implementation_failure と判定 → FailureReport → 実際の Codex（gpt-5.6-terra）が別ブランチで修正（`0122372` → `81d1cd9`）→ 新しい spec で再実行し合格。往復1回、実行ディレクトリは別 |
| ワーカーの再起動からの復旧 | 25 秒かかるジョブの実行中に orchestrator を SIGKILL → shim とジョブは動き続けた → 別のプロセスで resume すると台帳を照合して running を確認し、引き取って完了（投入は1回だけ） |
| 実 LLM Analyst（gpt-5.6-terra） | 事実（metrics と run を根拠として記載）、推論、限界、未解決点を分けて出力し、提案は0件を選んだ。計画にある「cc-pVDZ」の不整合（テスト用の Fake の計画に由来）を未解決点として指摘した。$0.0203 |
| 実 LLM Scientific Reviewer（gpt-5.6-sol）＋ ScopeAssessment（Analyst が提案しなかったので、手書きの下書き2件で実施） | 範囲内の再計算案：revise（受け入れ基準に参照値との許容差が入っていない）→ requires_human=true。基底を変える案：reject（STO-3G の不変条件に反する）→ 決定論的なチェックでも invariant の変更を検出。合計 $0.0210 |

**Fake でのみ確認したこと**：資源超過による再試行、インフラ障害の再試行と上限、2往復の上限、NaN / 破損 / 数値チェックの不合格の振り分け、提案の重複防止と件数・連鎖の深さの上限、Analyst が提案を出したときの Reviewer の流れ（実 LLM では Analyst が提案を出さなかったため）、Slurm の全操作。

### Phase 4 Acceptance（指示 §10 / §12）

| 項目 | 判定 |
|---|---|
| schema の検証、ComputeBackend の5操作、重複 submit、worker の再起動 | Passed（Fake と実プロセス） |
| 資源の検出と上限、独立した実験の並列と依存の直列 | Passed（実環境と Fake） |
| 成果物の破損、NaN / Inf、科学的制約違反、再試行 | Passed（Fake と validator の実データ） |
| Phase 3 での修正と2往復の上限 | Passed（実際の Codex で1往復、上限は Fake） |
| 予算（LLM は単価が必須、上限あり） | Passed（Fake と実際の費用記録） |
| 提案の審査と重複防止、ScopeAssessment の専用テスト | Passed（Fake ＋ 実 Reviewer 2件） |
| Reviewer が accept しても Phase 4 は承認しない（`pending_phase5_policy`） | Passed |
| ReviewedExperimentProposal の JSON roundtrip と Phase 5 handoff | Passed |
| LocalBackend の実ジョブ（正常、並列、依存、失敗 → 修正 → 再実行、worker の再起動） | Passed（実環境） |
| 実 LLM と Fake の区別 | 上の表のとおり |
| TSUBAME | Fake Slurm まで（接続していない） |
| Phase 1〜3 の regression | Passed（269 件） |

### Phase 3（2026-10-10、MacBook）

**自動テスト**：`uv run pytest` で **218 passed**（Phase 1 は 25、Phase 2 は 98、Phase 3 は 95）。Phase 3 のテストは Fake Backend / Fake Reviewer と、使い捨ての Git リポジトリで実行した。Phase 2 の handoff は、Phase 2 の orchestrator を apply モードで実際に動かして作った。

**実 CLI による統合テスト**：使い捨ての研究リポジトリ `ra-h2-research`（scratchpad 内、PySCF 2.14 / pytest / ruff / mypy を含む venv）で実施。

- Linear / Notion は変更していない（追加仕様の制約）。Phase 2 の apply と、承認ゲートでの Linear / Notion の確認は Fake で代用した。
- Codex / Claude / reviewer / git / 検証コマンドは本物を使った。

| テスト | 結果 |
|---|---|
| 使えるモデルの確認 | Codex：gpt-5.6-luna / terra / sol、Claude：haiku-4-5 / sonnet-5-5 / opus-5-5。いずれも実際に応答した |
| 実際の Codex で実装（H2/STO-3G） | **completed**。試行1は ruff 違反で revise → 試行2で全検証を通過し、実際の reviewer（gpt-5.6-sol、high）が pass。commit `297a656`、main は変更なし、223秒、63万トークン（費用は unknown） |
| ExperimentSpec | 構造化された argv、資源の推定値と根拠、単位つきの出力、numeric_checks 5件、scientific な入力 `configs/h2.json`、invariants は Notion から逐語転記 |
| （範囲外の追加確認）生成されたスクリプトを固定 commit で実行 | RHF −1.11676 / FCI −1.13728 / 相関 −0.02052 Hartree（H2/STO-3G、0.74 Å の既知値と一致） |
| Codex の quota 到達（**模擬**）→ 実際の Claude に fallback | **completed**。切り替え後、実際の reviewer が「0.74 Å の invariant が保証されていない」を検出して revise → Claude（sonnet-5-5）が直して pass。Claude の費用は $0.18 + $0.11 |
| FailureReport（implementation_failure、出力形式の不足）→ 実際の Codex で修正 | **completed**。試行1で pass。新しい commit `4bf73ed`、元のブランチ `297a656` は保持、scope は h2_energy.py / tests/** / experiment.json のみ、configs/h2.json は保護、新しい spec は元の spec を `supersedes_spec_id` で参照、予算は元の run から引き継ぎ（63万トークン） |
| 統合テストの不備で起きた blocked（記録） | 修正テストの1回目は、テストスクリプトの Fake Linear が Todo のままだったため、承認ゲートが正しく blocked にした。同じ report_id の再送は重複防止で再実行されないので、別 ID で再実行した |
| Prefect UI | Mac CLI → ローカル Compose の Prefect に flow run `implement-INU-273` と artifact `impl-inu-273` が記録された（INU-273 の handoff は Phase 3 より前のものなので blocked / not_approved。Linear / Notion は読み取りのみ） |

**Fake だけで確認したこと（実 CLI では未確認）**：

- 実際の quota / レート制限の検出（判定ロジックは文字列パターン。実際に quota に到達した場合の出力は未確認）
- Claude で quota に到達した場合
- 旧プロセスの停止確認
- crash からの復旧（`--resume`）
- 予算超過
- 往復回数の上限
- 重複 report（実 CLI の文脈でも1件は重複防止が働いた）
- 科学的入力を変更する修正の拒否

### Phase 3 Acceptance Criteria

| # | 条件 | 判定 | 根拠 |
|---|---|---|---|
| 1 | Phase 2 の handoff から実行できる | Passed | 実 CLI テスト、単体テスト |
| 2 | タスク単位の独立した worktree | Passed | 実際の研究リポジトリに3本のタスクブランチ、main は変更なし |
| 3 | 難易度と科学的リスクを別々に評価 | Passed | 単体テスト。実際の H2 は medium / high（調整後） |
| 4 | Implementer と Reviewer のモデル選択 | Passed | 単体テスト。実際は Implementer が terra / sol、Reviewer が sol |
| 5 | Codex CLI で実際にコードを生成 | Passed | 実 CLI |
| 6 | 静的検証と独立した LLM Reviewer | Passed | 実 CLI（ruff 違反の検出、sol によるレビュー） |
| 7 | 問題を検出して修正フローに戻す | Passed | 実 CLI（lint、invariant 違反）、単体テスト |
| 8 | 修正は最大2回 | Passed | 単体テスト |
| 9 | Codex → Claude の fallback | Passed（quota は模擬） | Claude は実際に動作した。quota の検出は Fake と文字列判定 |
| 10 | main を無断で変更しない | Passed | 実 CLI、単体テスト、監査ログ |
| 11 | 予算超過・権限不足で停止 | Passed（Fake） | 単体テスト |
| 12 | manifest から復旧 | Passed（Fake） | 単体テスト（crash → resume）。実 CLI では未確認 |
| 13 | Prefect UI で履歴を確認 | Passed | ローカル Prefect API に flow run と artifact |
| 14 | Phase 4 向けの固定 commit と artifact | Passed | 実 CLI（spec と handoff） |
| 15 | 既存のテストがすべて通る | Passed | 218 passed |

| 追加仕様の条件 | 判定 |
|---|---|
| ExperimentSpec の生成 | Passed（実 CLI ＋ 単体テスト） |
| Phase 4 が追加推論なしで起動条件を取得 | Passed（spec だけで、argv / env / inputs / resources / outputs / validation が揃う。範囲外の追加確認で実際に実行できた） |
| 固定 commit との整合 | Passed（commit に対して cat-file で検証） |
| FailureReport から修正を依頼 | Passed（実 CLI ＋ 単体テスト） |
| 科学仕様を守る修正だけを自動承認 | Passed（Fake：分類・scope・science guard。実 CLI では許可されたケースのみ確認） |
| 修正後に Reviewer が再評価 | Passed（実 CLI） |
| 修正前の成果物を保持 | Passed（実 CLI ＋ 単体テスト） |
| 差し戻し回数と予算上限 | Passed（Fake） |
| Phase 2 との後方互換 | Passed（単体テスト：古い handoff が読める、`phase2_mode` がなければ blocked） |
| 既存の全テストが成功 | Passed |

### Phase 2（2026-10-10、MacBook）

| 確認 | 結果 |
|---|---|
| `uv run pytest` | **116 passed**（Phase 1 の 25 件、Phase 2 の 91 件） |
| 実データでの読み取り（INU-265 / 261 / 239 / 243） | ゲートが期待どおりに判定した（スコープ外、In Progress、Backlog と Pending、見出しの不足） |
| ローカル Compose の worker：DB `research_autopilot` 作成、`db init`、deploy、`plan-task` の check-only（INU-265） | blocked。PostgreSQL に記録された |
| 統合テスト INU-271（仕様不足） | insufficient_spec（プレースホルダーを検出、LLM 呼び出しなし） |
| 統合テスト INU-272（Pending） | blocked / not_approved（LLM 呼び出しなし） |
| 統合テスト INU-273 の dry-run（worker 上） | ready。Planner 21秒 / Reviewer 7秒で approved、入力 4,639 / 出力 2,625 トークン、handoff JSON を作成、Linear は変更なし |
| `--next` の check-only | 271 と 272 を飛ばして INU-273 を選んだ |
| INU-273 の apply（ユーザー承認あり、Mac の CLI → Prefect） | ready、Linear が Todo → In Progress（updated）、監査ログ1行 |
| INU-273 の apply 2回目 | blocked / invalid_status。書き込みなし（監査ログは1行のまま） |

まだ確認していないこと：Eagle での動作、ntfy への通知、単価を設定したうえでの料金推定（単体テストでは確認済み）、Reviewer の revision_required → 再計画の実 LLM での発生（単体テストでは確認済み）。

### Phase 2 Acceptance Criteria

| # | 条件 | 判定 |
|---|---|---|
| 1 | 実際の Notion / Linear から取得できる | Passed |
| 2 | 着手できるタスクを決定論的に選べる | Passed（`--next`） |
| 3 | Planner が構造化された計画を作る | Passed |
| 4 | Reviewer がすべての計画を検証する | Passed（実行時と単体テスト） |
| 5 | 最大2回まで再計画できる | Passed（単体テスト。実 LLM では再計画が起きなかった） |
| 6 | 4状態を区別できる | Passed（ready / blocked / insufficient_spec は実データ、transient_failure は単体テスト） |
| 7 | 許可済みのタスクを In Progress にできる | Passed（INU-273） |
| 8 | 計画の完了だけで Done にしない | Passed（state type が started 以外への更新を拒否。テストあり） |
| 9 | 重複実行を防げる | Passed（リース、apply の2回目が blocked） |
| 10 | 履歴、根拠、トークン数を確認できる | Passed（`runs list/show`、PostgreSQL、JSON） |
| 11 | Prefect UI で履歴を確認できる | Passed（ローカル Compose の UI / API に flow run と artifact がある。Eagle の UI では未確認） |
| 12 | 単体テストと安全な統合テストが成功する | Passed |
| 13 | Phase 3 用の構造化 artifact が作られる | Passed（`handoffs/<task>/<plan_id>.json`、schema_version 1） |

### 研究室サーバーでの確認（2026-10-10、ユーザー実施・報告）

ユーザーが `main`（`e623fcb`）を研究室サーバーに pull し、運用ドキュメントの手順で次を確認したと報告した。手順とドキュメントの間に食い違いはなかったとのこと。エージェントはサーバーに直接アクセスしていないので、個々のコマンド出力は見ていない。

- Compose の3サービスの起動、worker による deployment の実行（AC-03 / 04）
- MacBook から Tailscale 経由での登録・起動（AC-05）
- スマートフォンから Tailscale 経由で UI の状態・ログを確認（AC-06）
- 再起動後の履歴保持、バックアップと復元（AC-07 / 10）

まだ確認されていないこと：本番値（180 秒 / 540 秒）での zombie 検出の実測、実際の ntfy サーバーでの通知、未承認経路からの負の確認の個別結果、サーバーの OS / CPU。

### Acceptance Criteria

| ID | 判定 | 根拠 |
|---|---|---|
| AC-01 | **Passed** | `uv run pytest` 25 passed（MacBook、merge 後の main でも再実行） |
| AC-02 | **Passed** | MacBook で正常系・異常系の flow を実行（ローカルの一時サーバーとローカル Compose の両方） |
| AC-03 | **Passed** | 研究室サーバーで確認（ユーザー報告）。MacBook の Compose でも healthy を確認済み |
| AC-04 | **Passed** | 研究室サーバーで確認（ユーザー報告） |
| AC-05 | **Passed** | MacBook → Tailscale → 研究室サーバーで確認（ユーザー報告） |
| AC-06 | **Passed** | スマートフォンから Tailscale 経由で確認（ユーザー報告） |
| AC-07 | **Passed** | 研究室サーバーで確認（ユーザー報告）。ローカルでも server / worker / postgres の再起動と down → up で確認済み |
| AC-08 | **Passed** | ローカル Compose で retry・失敗ログ・failed 通知（モック受信器）を確認。実際の ntfy では未確認 |
| AC-09 | **Passed** | `test_idempotency.py`（16スレッド同時）、`test_duplicate_runs_apply_side_effect_once` |
| AC-10 | **Passed** | 研究室サーバーで確認（ユーザー報告）。ローカルでも構築・バックアップ・リハーサル復元・in-place 復元を再現済み |

## Known Issues / Blockers

1. **Notion / Linear へのアクセス不可**：エージェントは外部仕様を取得できない。Phase 1 の仕様はローカルの `docs/phase_specs/phase1.md` を使う。
2. **実環境の情報が記録されていない**：研究室サーバーの OS / CPU、Docker / Compose のバージョン、バックアップの保存先、通知エンドポイント。サーバーでの動作はユーザーが確認済み。
3. **実行中の run は自動で復旧できない**：heartbeat automation で Crashed を検出し、副作用を確認してから手動で再実行する設計（recovery.md）。
4. **`docker kill` / `docker stop` した worker は自動で再起動しない**（Docker の仕様。ローカルで確認済み）。運用者が `up -d` で起動する。
5. **worker ごと落ちた run は hook 通知が飛ばない**：Crashed への変更は automation が行うため。必要なら Prefect automation の通知 action を追加する（Phase 1 では未実装）。
6. `data/` の扱いは未決定。Phase 1 では研究成果物の storage を追加していない。
7. `docs/phase1_technical_design.md` §1 と旧 handoff にある「phase1.md は存在しない」は古い記述（現在は存在する）。設計書は Codex の文書なので変更していない。
8. ローカル検証用のスタック `ra-verify`（コンテナ、ボリューム `ra-verify_pgdata` / `ra-verify_worker-state`）とローカルの `.env` が MacBook に残っている。不要になったら `docker compose -p ra-verify --env-file .env -f infra/compose.yaml down -v` で削除する（ローカル検証データのみ）。

## Exact Next Actions

1. ユーザーの希望があれば、テスト専用 approval request で実 Discord の3ボタンと revision modal を操作し、polling / Linear 反映 / message disable まで確認する。本番タスクは使わない。
2. Eagle デプロイ：`docs/operations/eagle_wsl_setup.md` の順に進める。
   - 人間が H1〜H4 を行ったら、エージェントが §3 A1 を実行する。
   - 次に人間が H5、そのあとエージェントが A2 を実行する。
   - H4 最後の `docker run` が失敗したら、Phase 9 の制限オプション（`external/sandbox.py` の `_limits` / `_hardening`）を rootless 用に見直す。
   - A1 は完了（2026-10-14）。ユーザー指示（2026-10-14）で `docs/wsl-setup` を main に fast-forward し、origin に push した（`5335f57..beb2f1b`）。次は人間が H5 を行い、そのあとエージェントが A2 を実行する。
2b. TSUBAME と Phase 5 の本番化（schedule active、TEST_ONLY=false）は `phase5_autopilot.md` §7 に従い、ユーザーの承認を得てから行う。
3. テストデータの後片付け（INU-270〜273、INU-279、Notion のテストページと Notes、ラベル `autopilot`）と、Mac の検証用環境で PostgreSQL を 5433 で公開している override を外すかどうかは、ユーザーが判断する。
