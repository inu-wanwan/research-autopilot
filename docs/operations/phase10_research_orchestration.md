# Phase 10 Autonomous Research Planning & Orchestration

仕様は `docs/phase_specs/phase10.md`（ユーザー提供の指示＋正本 Notion INU-288 の該当節＋ユーザー決定）。

## 実装計画（2026-10-11）

既存 Orchestrator（Phase 5 engine）の拡張として実装する。新しい Orchestrator・承認システム・状態管理は作らない。

| 責務 | 実装 | 再利用する既存機能 |
|---|---|---|
| 起動 | Run Autopilot（6A）で研究目的タスクを受理。研究目的の見出しを持つタスクを research objective として扱う | 6A ExecutionRequest / ExecutionRun、engine.poll |
| Research Planner（10A） | 研究スコープの構造化（各項目に原文引用を要求し決定論的に照合。引用のない値は確定しない）→ Evidence / Artifact Context（Phase 7 Catalog・Phase 8 レポート・Phase 9 Registry）→ 根拠付き計画と Task DAG draft（決定論的検証） | Phase 8 の引用照合方式、Catalog / literature / external の照会 |
| ResearchAuthorization | Phase 5 の `ResearchAuthorization` に version・内容 hash・研究スコープ・計画 ID を追加。旧版は superseded | Phase 5 authorization / policy gate |
| 承認 | `ApprovalKind.RESEARCH_SCOPE`。承認対象 hash＝計画内容 hash（Notion 仕様本文 hash を含む）。古いボタン・版違い・仕様編集後は stale | ApprovalController（Discord 本人確認・期限・二重押下防止）、Linear Awaiting Approval |
| Notion | 計画は研究目的ページ内のサブページ「RA Research Plan vN」（不変、版ごとに新規）。`Approval Status` は承認イベントの projection（失敗時は再試行、表示を根拠に実行しない） | NotionWriter |
| 実行（10B） | 承認後、サイクルごとに子タスク（Notion：Origin=RA・親タスク relation、Linear：blocked-by）を作り、6B の ParentRun / TaskDAG で実行 | 6B dag_runtime（schedule_ready・artifact handoff）、Phase 2〜4 の各タスク実行 |
| Research Evaluator / Replanner（10B） | サイクル完了時に検証済み結果から次の行動（追加実験・補助解析・再試行・完了・停止・スコープ変更案）を根拠付きで決定。決定論的ゲートで承認範囲（条件範囲・件数・サイクル数・予算・不変条件）を確認し、範囲外は変更案（新しい版）として Discord 承認待ち | Phase 4 Analyst の結果、policy / budget |
| 予算 | chain 予算（Phase 2〜4・文献・外部導入）に計画／評価の LLM 費用を加算 | autopilot.budget.chain_usage |
| 停止・再開 | Discord の停止／再開ボタン（承認者のみ）。停止は新規投入停止、実行中ジョブは完了まで | Discord gateway の本人確認 |
| 通知 | 承認依頼・マイルストーン（計画承認・サイクル評価）・成果・異常のみ。研究の子タスクごとの通知は抑止 | notify_once / Discord ルーティング |

テスト：10A（根拠付き計画・不足情報を確定しない・承認前実行拒否・stale 承認拒否）、10B（複数サイクル・範囲外／仕様変更／予算超過の拒否・停止／再開／再試行での整合性）、共通（Notion / Discord / PostgreSQL / Linear の整合性）。E2E：10A は Phase A の研究目的で計画のみ、10A＋10B は INU-292（H₂ RHF–FCI）。

## 使い方（要約）

1. 研究目的タスク（Notion Tasks）に `## Research Objective` / `## 研究目的` 見出しで問い・対象・主指標・許容条件・最大点数などを書き、`## Budget` / `## 予算`、`## Scientific invariants`、`## Acceptance criteria` を書く（任意：`## 自律実行の裁量`）。`Specification` 見出しは不要。
2. Run Autopilot を押す → 10A：RA が計画 vN を作り、タスク内サブページ「RA Research Plan vN (hash)」に記録、`Approval Status`=Pending、Discord に Notion URL・版・hash 付き承認依頼。必須項目（目的・主指標・不変条件・予算・成功条件・条件範囲）が仕様から確認できない場合は承認依頼を出さず Blocked（`needs_input`）。
3. Discord で承認 / 却下 / 修正依頼。承認＝`ResearchAuthorization`（version・content hash・scope）。仕様編集や新しい版で古いボタンは stale。修正依頼はコメント付きで v(N+1) を自動再提示。
4. 10B：サイクルごとに子タスク（Origin=RA、親タスク relation、Linear blocked-by）を作り 6B DAG で実行 → Research Evaluator が次の行動を決定 → 決定論的ゲート（条件範囲・最大点数・予算・サイクル数・再試行・進展なし）。範囲外は scope change 版として Discord 承認待ち。完了時は Notes（分類 結果）・Catalog（`ra-research`）・Discord results に報告。
5. Discord：Stop（サイクル評価メッセージ）＝新規投入停止、Resume（停止・異常メッセージ）＝停止解除／Blocked な研究の再開（最後のサイクルを HUMAN_RESUME 付きで再評価、進展なしカウントはリセット）。CLI 代替：`research-agent research stop|resume|status|plan|revoke|plan-dry-run`。

## E2E 実施記録（INU-292、2026-10-11 / Mac 実サービス）

| 項目 | 結果 |
|---|---|
| 10A dry-run（Phase A INU-274） | ✅ 予算・必須 baseline 手法・offline oracle 検証・実行前提を blocking として検出、タスクを作らず（$0.02） |
| 10A INU-292 | 計画 v1（needs_input：LLM の blocking 誤判定）→ v2 / v3（invalid：範囲境界未抽出・結果依存タスク・max_count 誤用）→ v4 承認（**自律上限 1/0/0/0 が未照合のまま承認された不具合**、後に取り消し）→ v5 承認（「1回以上」を上限 1 と誤読、取り消し）→ **v6 承認（authorization v3、RA default 上限 5/3/2/2）**。いずれも原因は修正済み。旧版・取り消しは audit として保持 |
| 承認の整合性 | ✅ Discord 本人（`discord:818844326578356275`）、版 hash、Notion Approval Status projection（Pending→Approved）、Linear Awaiting Approval→In Progress |
| サイクル 1（承認 v1、INU-293〜295） | INU-293 の計算は成功（4 点）したが Phase 4 validator がネスト結果を読めず不合格 → 上限 1/0/0 により停止 |
| サイクル 2（承認 v3、INU-296〜298） | INU-296 が Phase 2 で差し戻し（子仕様に距離なし：仕組みの不具合） |
| サイクル 3（INU-299〜300） | INU-299 の 5 点計算は成功（PySCF 2.14、ΔE(0.5 Å)=0.01216 Eh、provenance 保存）したが experiment.json の units / metric 宣言誤りで不合格 → Implementer 規則を追加。Resume 後の再評価で continue |
| サイクル 4（INU-301 計算再実行 → INU-302 解析） | INU-301 がレビューで停止（実装・検証契約がリポジトリに無い）→ 評価 continue |
| サイクル 5（INU-303 実装・計算 → INU-304 解析） | INU-303 の Phase 3 は成功（`d10f919`）したが Phase 4 で `python_version: "3.12.x"` を uv が解決できず停止（`54a9112` で正規化）。同一サイクル内再試行（`4613757`）の 2 回目は Linear が In Progress のまま残り Phase 2 開始ゲートで即拒否（`542e590` で修正）、3 回目は Codex サブスク（〜02:37 JST）と Claude（〜00:10 JST）の利用上限で停止。2026-10-12 00:14 JST の Resume で 4 回目：Codex サブスク上限 → **API 課金 Codex（`codex_api`、gpt-5.6-sol）に fallback して実装・レビュー中**（00:21 JST 時点、attempt 1 は reviewer revise、attempt 2 実行中、Phase 3 費用 $0.79）。INU-304 は未実行 |
| モデル比較（読み取り専用 A/B、各 2 回 $0.28） | 修正後は terra / sol とも合格・自律上限正常。sol は「おおむね5点」に対し 3 点（仕様からの逸脱）。モデル切替は不要と判断（ユーザーに報告済み） |

未完了（INU-292 Acceptance）：検証済み結果での評価→追加サンプリング（Evaluate→Replan→Execute の観測）、Notes / Catalog 保存と Discord 完了通知、SSOT 整合性の最終確認。

### 操作者による介入の記録

`research_controls` / authorization に記録済み：`cli:claude-code` による resume（不具合修正後の再開、計 5 回）、stop（修正中の一時停止、2 回）、revoke（承認 v1：自律上限未照合、承認 v2：「1回以上」誤読）。いずれもユーザーの依頼または同意の範囲。

## 引き継ぎ：E2E の続け方（Codex 向け）

前提：`.env` は設定済み（`RA_APPROVAL_MODE=discord`、`RA_NOTIFICATION_BACKEND=discord`）。scheduled poll（Prefect）は無効なので、ポーリングは手元で回す。

1. 状態確認：
   - `uv run research-agent research status INU-292`（計画版・承認・サイクル・決定・controls）
   - `uv run research-agent autopilot status`（WorkflowRun の stage / blocked 理由）
2. Discord gateway（承認・Stop/Resume ボタンの受信）が動いていなければ起動：`uv run research-agent discord run`（常駐）。
3. ポーリング（書き込みは `[TEST]` / `[E2E]` タイトルのみ）を回す。30 秒ごと・最長 4 時間の例：

```zsh
cd /Users/callduck/workspace_callduck/dev/ResearchAutopilot
export RA_AUTOPILOT_WRITE=true RA_AUTOPILOT_TEST_ONLY=true
end=$((SECONDS + 4*3600))
while (( SECONDS < end )); do
  uv run research-agent autopilot poll --local 2>&1 | tail -c 2000 | tr '\n' ' '; echo
  st=$(uv run research-agent autopilot status | python3 -c "import json,sys;d=json.load(sys.stdin);print(next((r['stage'] for r in d['runs'] if r['task']=='INU-292'),''))")
  echo "STAGE INU-292: $st"; [[ "$st" == done || "$st" == cancelled ]] && break
  sleep 30
done
```

   `--task` は付けない（付けると子タスクが進まない）。他に active な run が無いことを `autopilot status` で事前確認する。
4. Blocked で止まったら：`research status` の最後のサイクルの decision / gate_problems、子タスクは `autopilot status` の blocked 理由と実験 manifest（`store.get_experiment(<id>)` の validation checks）を確認。仕組みの不具合なら修正・テスト・commit してからユーザーに Discord の Resume を依頼（ユーザー不在時のみ `research-agent research resume INU-292 --actor "codex (<理由>)"`）。
5. 完了したら INU-292 Acceptance 1〜6 を確認し、この節の表を更新、`docs/development_handoff.md` を更新してユーザーに報告。main merge / push はユーザー指示待ち。

### 2026-10-12 の仕組み修正（Claude Code）

- `12807f6`：停止通知の dedupe キーが WorkflowRun 単位で、Resume 後の 2 回目以降の停止が通知されず Resume ボタンが届かなかった → 停止ごと（execution run 単位）に通知。Resume ボタン付きメッセージは **approvals チャンネル**へ。
- `542e590`：同一サイクル再試行時、blocked 子の Linear started 状態を Todo に戻す。開始ゲートで作業前に拒否された再試行は `max_retries_per_task` に数えない（ユーザー決定、上限値は不変）。
- `75daadc`：Phase 3 fallback を連鎖化（`RA_IMPL_FALLBACK_BACKEND`、既定 `claude,codex_api`、最大 2 回、使えない先はスキップ、reviewer も同順）。`codex_api` = `OPENAI_API_KEY` を `CODEX_API_KEY` として専用 `CODEX_HOME`（`~/.research-autopilot/codex-api-home`）で `codex exec` に渡す API 課金 Codex。費用は `RA_MODEL_PRICING` で毎回計上、料金未登録モデルは起動拒否。利用上限（quota / rate limit）で中断した再試行は回数に数えない（ユーザー決定）。実 API で最小呼び出しを確認（$0.024）。
- `b69f8bd`：再試行時、利用上限・一時障害・プロセス障害・時間切れで止まった子は **同じ計画と worktree で Phase 3 を継続**、Phase 3 完了後に Phase 4 環境構築で失敗した子は **Phase 4 のみ再実行**。計画再利用前に Phase 3 承認ゲート（Notion 仕様が handoff から不変か等）を再確認し、不合格なら従来どおり再計画。費用・トークンは継続 run に引き継ぐ。
- `.env`（Git 管理外）：`RA_IMPL_FALLBACK_BACKEND=codex_api`（ユーザー指示：手元開発用に Claude の枠を残すため、研究パイプラインでは Claude を使わない）。
- 検証：`uv run pytest` 550 passed / 1 warning（既存 discord/audioop）。継続再開（b69f8bd）は fake backend の統合テストのみで、実データでは未検証。

### INU-292 の打ち切りと INU-305（2026-10-12）

- INU-292 は最大サイクル（5/5）に達しており、受け入れ条件（追加サンプリングのサイクル）を満たせないため、ユーザー判断で打ち切り。INU-292 と未完了の子 INU-293〜304 を Linear Canceled（操作者として API で変更、DB の linear_writes に理由を記録）、WorkflowRun を cancelled。
- 同じ仕様で **INU-305** を作成（Notion ページの仕様ブロックを複製、RA の計画サブページは複製しない）。10A の計画 v1 は API 予算が RA 既定 $5。ユーザーが予算行に「API 費用 10 USD」を追記 → v2 は別行の引用で ambiguous、v3 は同一行でも `_numbers` が 10 を 1 と正規化するバグで ambiguous（`d483b91` で修正）。v3 の needs_input 通知は errors に Resume なしで届き、Discord から再開できなかった（CLI の `research resume` をユーザー依頼で実行）。
- ユーザー決定（2026-10-12 01:4x JST）：UX 方針の確定 → 監査・修正 → E2E を最初から、の順で進める。

### INU-305 の E2E 結果（2026-10-12 05:27 JST 完了）

| Acceptance | 結果 | 根拠 |
|---|---|---|
| 1. 10A の根拠付き計画・承認・Notion 反映 | ✅ | 計画 v5 を Discord で本人承認（v1〜v4 は予算行の不具合・ユーザーの差し戻しで改訂）、承認履歴・Approval Status 反映 |
| 2. 承認前は実行しない | ✅ | 承認まで子タスクなし、承認後に既存 Orchestrator 経由で実行 |
| 3. 複数 geometry の収束済み RHF/FCI・ΔE と再現情報 | ✅ | INU-312（5 点）+ INU-316（2.5 Å）を Phase 4 で検証（PySCF 2.14.0、Python 3.12.2、STO-3G、収束設定・環境を保存）。ΔE：0.5 Å 0.0121635 → 2.5 Å 0.2331113 → 3.0 Å 0.2775836 Eh |
| 4. 結果に基づく追加サンプリング | ✅ | サイクル 3 の評価で「隣接差最大の 2.0–3.0 Å」→ 2.5 Å を追加（INU-316）→ 6 点集約（INU-317） |
| 5. Notes / Catalog / Discord 完了通知 | ✅（Evaluator は insufficient と記録） | Notes 作成、Catalog `ra-research` に research-report 登録、results に Notes URL 付き完了通知。Evaluator の判定は公開処理を子タスクの責務と誤解したため（`829bb95` で修正） |
| 6. SSOT 整合性 | ✅ | Linear：INU-305/312/316/317 Done、未使用の子 9 件 Canceled（理由付き）、承認は authorization v1（discord 本人） |

- 5 サイクルのうち 1・2・4 は仕組みの不具合で消費（Plan Reviewer の基準のぶれ、単位検証、上流成果物の受け渡し）。サイクル 5 でも入力マニフェストの相対パス・Evaluator の誤解で停止し、修正後に Resume（ユーザー 1 回、代行 1 回）。
- この E2E 中の修正：`f64bb23` `ec8eeb3` `56c2a89` `0d54083` `329e805` `fadf9b1` `bbbc1c8` `c20df64` `352dc68` `9940775` `829bb95`。
- 2 回目の E2E：INU-318（同一仕様）を 05:2x JST に開始、研究スコープ v1 を dev 限定 CLI 承認（`cli-dev:claude-code (delegated …)`）で承認。

### INU-318（2 回目の E2E、2026-10-12 06:3x JST 完了）

- 3 サイクルで complete。仕組みの不具合による停止 0 回（OpenAI API のクレジット切れ・プロジェクトの使用額上限による停止 1 回。補充後に代行 Resume）。
- サイクル 1：実装（INU-319）・初期 5 点計算（INU-320、0.5/0.74/1.0/1.5/3.0 Å）・解析（INU-321）がすべて 1 回で通過 → 1.5–3.0 Å の区間が粗いとして追加点を決定。サイクル 2：2.0 / 2.5 Å を計算（INU-322）、最終集約（INU-323）は API の利用枠切れで停止。サイクル 3：再試行（INU-329）で 7 点を集約・再検証 → complete。ΔE：0.0121635 Eh（0.5 Å）→ 0.277584 Eh（3.0 Å）。
- Acceptance：1〜4・6 ✅（研究スコープは dev 限定 CLI 承認 `cli-dev:claude-code (delegated …)`）、5 ✅（Notes 作成・Catalog 登録・results への完了通知。Evaluator の判定は not_assessable）。未使用の INU-323 は Canceled。

### 既知の課題（未解決）

- `codex_api` で実タスクを最後まで完了した例は未確認（実行中）。1 回の Codex 実行の途中では費用上限で止められない（呼び出し間で判定、1 実行は wall time 30 分で打ち切り）。エージェントが実行するコマンドに API キーが見えないことは Codex の既定ポリシー頼みで、実タスクでの確認はしていない。

- 「おおむね5点」など曖昧な数量は機械検査していない（承認時に人が確認、修正依頼で直す）。
- Codex の experiment.json 宣言と Phase 4 validator の取り決めは Implementer 規則で補強したが、実データでの再確認はサイクル 4 の結果待ち。
- 子タスクが前サイクルの実装コミットを引き継がない（各子タスクが独立に実装する）。Phase 3 の branch が main に merge されないため。サイクル間でのコード再利用は未実装（現状は再実装で動作）。
- 研究目的タスク自身の Notion `Execution Status` は Blocked→再開時に新しい ExecutionRun を作るため、過去の Blocked 記録が残る。
- Eagle（Linux）・本番 PostgreSQL での運用は未検証。
