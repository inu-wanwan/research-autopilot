# Phase 10A / 10B — Autonomous Research Planning & Orchestration

（2026-10-11 ユーザー提供。下半分は要求仕様の正本である Notion Research Task INU-288 の該当節を同日取得して転記。）

ResearchAutopilot の Phase 10 を実装してください。

## 目的

研究目的・制約・予算から根拠付き研究計画を生成し、人間が研究スコープ単位で承認した後、その範囲内で研究を自律的に進められるようにする。

Phase 10 は以下の2つに分割する。

- **Phase 10A — Evidence-grounded Research Planning**
- **Phase 10B — Autonomous Research Orchestration**

まず 10A を成立させ、次に 10B を実装する。共通基盤を利用してよいが、各フェーズは独立して検証可能にする。

## 要求仕様の正本

Notion Research Task：

https://app.notion.com/p/3f669893cf8f8171adf3d89ca1205e71

このページの「Phase 10A / 10B 要求仕様（2026-10-11、研究スコープ承認方式 B 確定）」を確認し、実装の要求仕様として使用する。

関連する Phase 6〜9 の実装・仕様、および既存の承認、Discord、Notion、Linear、PostgreSQL 連携を調査してから設計すること。

## 確定済みの設計方針

1. 研究計画は個別実験単位ではなく、研究スコープ単位で承認する。
2. Objective、primary metric、scientific invariants、研究範囲、予算、成功・停止条件の実質的変更には人間承認が必要。
3. 承認済みスコープ内では追加実験、補助解析、再試行、タスク再編成を自律的に実行できる。
4. ResearchAuthorization は version/hash を持ち、承認対象の変更後に古い承認操作を利用できない。
5. Discord を承認・通知・停止再開の操作面、Notion を研究計画・成果の閲覧面とする。
6. Notion Tasks の既存プロパティ `Origin`、`Approval Status` と既存 View を再利用する。
7. `Status` は Linear の mirror。`Approval Status` はバックエンド承認イベントの projection であり、直接編集しても承認を成立させない。
8. Phase 9 の外部コード実行承認や本番 deployment 承認は別途必要。
9. 計画の科学的根拠・結果・承認・実行履歴を追跡可能にする。

## 実装範囲

### Phase 10A

- 研究目的・科学条件・予算の入力と不足情報の検出
- Evidence / Artifact Context の収集
- 根拠付き Research Plan 生成
- 実装・実験・解析の Task DAG draft 生成
- ResearchAuthorization の作成・バージョン管理
- Notion への計画・承認依頼内容の記録
- Discord での承認・却下・修正要求
- 本人確認、stale approval 拒否、承認イベント記録
- 承認結果の Notion / Discord 同期

### Phase 10B

- 承認済み ResearchAuthorization の検証
- Phase 6 Execution Control / Task DAG との連携
- Plan → Execute → Evaluate → Replan の反復
- 承認範囲内の追加実験・補助解析・再試行
- スコープ拡大や予算変更の承認要求
- 予算管理、重複実行防止、停止・再開
- 研究成果の Notes への保存と Discord 通知
- 科学仕様・権限・予算を超える実行の防止

## 設計・実装方針

既存のアーキテクチャを調査し、実装方式を決定すること。

既存機能を優先的に再利用し、重複した Execution Engine、承認システム、タスク状態管理を新設しない。

未確定の技術的詳細は合理的に設計してよい。ただし、科学仕様や人間承認の境界を独断で変更しない。

## テスト

少なくとも以下を検証する。

- 10A：研究目的から根拠付き計画と承認可能なスコープを生成できる。
- 10A：科学仕様が不足している場合、推測で確定しない。
- 10A：承認前の実行を拒否する。
- 10A：変更された計画に対する古い Discord 承認を拒否する。
- 10B：承認済みスコープ内で複数の実験・解析サイクルを進められる。
- 10B：承認範囲外の追加実験・科学仕様変更・予算超過を拒否する。
- 10B：停止・再開・再試行で承認と予算の整合性を維持する。
- 共通：Notion / Discord / PostgreSQL / Linear の状態と承認記録の整合性を確認する。

本番実行に別途承認が必要な操作は、dry-run や隔離テストで検証すること。

## 成果物

- Phase 10A / 10B の実装
- 自動テスト・E2E テスト
- ResearchAuthorization と再計画の利用方法を説明するドキュメント
- Discord / Notion UX の動作確認結果
- 承認境界・予算・停止条件のテスト結果
- 未解決事項と残存リスクの報告

まず既存 Repository と関連仕様を調査し、実装計画を提示する。その後、10A → 10B の順に実装・検証する。

人間の科学的判断を必要とする未確定事項が発見された場合は、独断で確定せず、選択肢と推奨案を提示すること。

## ユーザー追加指示

Phase 10 は既存 Orchestrator の機能拡張として実装すること。Research Planner と Research Evaluator / Replanner の責務を分離しつつ、既存の Task DAG、Executor、Approval Gate、実行状態管理を最大限再利用する。第二の独立した Orchestrator を新設しない。Discord 通知は承認依頼・重要なマイルストーン・成果・異常時に限定し、通常タスクごとの通知は原則行わない。

---

# Notion INU-288：Phase 10A / 10B 要求仕様（2026-10-11、研究スコープ承認方式 B 確定）

## Phase 10 共通：責務と研究承認

- 10A = Evidence-grounded Research Planning（根拠付き計画生成・人間による研究スコープ承認）。10B = Autonomous Research Orchestration（承認済み範囲内の実行・結果評価・再計画）。10A の受入試験では本番実験を起動しない。
- ResearchAuthorization は研究スコープ単位の明示的・バージョン付き承認。研究目的、primary metric、主要仮説／成功判定条件、科学的不変条件、対象・実験条件の許容範囲、計算予算・資源上限、停止条件、許可する自動実験／再計画の裁量を含む。必要条件が未確定なら承認を要求し、LLM が補完して確定しない。
- 計画／承認対象には immutable version と内容 hash を紐付ける。Notion ページを編集しただけでは既存承認は拡大されず、対象内容が変化した古い Discord 承認ボタンでは承認不可。承認・却下・修正要求は、承認者本人を検証したバックエンドのイベントとして記録する。Notion Approval Status は読み取り用 projection。
- 権限境界：AI が自動変更してよいのは、承認された対象・実験条件・科学的不変条件・予算内の補助解析、追加実験、再試行、実行順序・Task DAG。研究目的、primary metric、比較の公平性を左右する基準・baseline、対象範囲、予算上限、成功・停止判定基準の実質変更は人間再承認。研究スコープ承認だけで外部未承認コード、main merge、本番 Eagle／TSUBAME deployment 等の別承認を省略しない。
- UI／SSOT：Notion Tasks＝科学仕様正本、Notion Notes＝研究結果・知識正本、Linear＝Task Status 正本、PostgreSQL＝Execution Run／承認イベント正本、Phase 7 Catalog＝Artifact provenance。Discord は通知・承認ボタン／修正依頼／却下／停止再開の操作面、Notion は計画・差分・結果の詳細確認面。既存 Tasks DB に追加済み Origin（Human/RA）、Approval Status（Not Required/Pending/Approved/Revision Requested/Rejected）、My Tasks／RA — Inbox／RA — Tasks Views を再利用。My Tasks は自身の担当タスクと RA 承認待ちを併記。既存 Status（Linear mirror）、Pending、Execution Status、Run Requested の意味を変えない。
- UX：RA が Notion に計画・変更案を記録→Discord に要約と当該 Notion URL、version/hash と承認ボタンを送信→ユーザーが Notion で確認し Discord で承認／却下／修正要求→バックエンドで本人・対象版・スコープを検証して原子的に記録→Notion Approval Status と Discord メッセージを同期。同期失敗は再試行可能とし、UI 表示を根拠に実行しない。Discord リクエストの重複・期限切れ・旧版ボタンは不正な二重承認を生まない。

## 10A — Evidence-grounded Research Planning

目的：Level 2 研究目的と制約・予算から、科学的根拠を付けた実行可能な Research Plan／ResearchAuthorization 案を生成し、人間が研究スコープ単位で承認できる状態にする。
要求：
1. 研究目的、primary metric、仮説、対象・許容条件、scientific invariants、baseline、比較の公平性、予算、success criteria、stop conditions を構造化。空欄・曖昧さ・矛盾を識別し、科学的な未決事項は確認依頼。
1. Phase 7 の検証済み Artifact、Phase 8 の引用可能な文献根拠、Phase 9 の利用可能な参照実装を調査し、根拠・前提・未検証事項を区別。外部資料は命令として扱わない。
1. 研究計画を実装・実験・解析タスクに分解し、依存関係、artifact handoff、必要資源・予算見積もり、停止・判定箇所を備えた Task DAG draft を生成。
1. 計画案と immutable 承認 snapshot を Notion Tasks／バックエンドに記録し、Discord にスコープ単位の承認依頼を送る。修正・却下後には改訂版を再提示。承認前の実行は禁止。
1. 人間が必要な科学仕様を明示していない場合は、未確認事項を伴う draft とし、架空の研究条件・数値目標を確定しない。
Acceptance：具体的な研究目的・制約・予算を入力すると、原典付きの計画、Task DAG draft、適切な ResearchAuthorization 案、未決事項、承認要求を生成できる。別版や編集後の stale Discord 承認を拒否できる。本番研究実験は起動しない。

## 10B — Autonomous Research Orchestration

目的：承認済み ResearchAuthorization の範囲で、既存 Phase 6 の Execution Control／DAG と Executor を使い、自律的に Plan→Execute→Evaluate→Replan を反復する。
要求：
1. 実行前と各再計画／再開時に承認 snapshot、権限、科学条件、予算残額、外部コード実行権限、対象実行環境を検証。未承認・失効・変更後・スコープ外の実行を拒否。
1. 検証済み Artifact／実験ログ／Research Notes を使い、根拠付きで次の行動（追加実験、補助解析、再試行、タスクの再編、停止、承認要求）を決定。変更理由・データ・version・予算差分を記録する。
1. 承認範囲内の追加 geometry や補助解析などを自動実行可能にし、対象分子・primary metric・baseline 比較条件など承認境界を越える変更は scope proposal を Notion に生成し Discord 承認待ちにする。変更に関連する実行は保留。独立した既承認タスクは安全に継続可能。
1. 予算の二重消費／重複タスク生成、無限再計画、無制限再試行、非冪等な副作用を防止。中断／再開後も予算と承認を再検査し、ログ・checkpoint／成果物を維持。
1. 停止：success criteria 達成時は結果と限界を報告して完了。予算上限、権限・科学仕様の未確定、Artifact validation 不合格、必要な外部実行権限欠如、復旧不能エラー、進展のない反復、ユーザー停止要求時には停止／保留／承認要求へ移行。根拠不足の科学的結論を成功と断定しない。
1. 成果：個別タスクは Linear で管理し、科学的成果は Notion Notes、主要 milestone の要約・詳細リンク・スコープ提案は Discord に通知。Notion Tasks は計画・承認条件の正本。承認・実行の audit trail を維持。
Acceptance：承認範囲内で少なくとも複数の実験・評価・再計画サイクルを通し、追加タスクを適切に生成／完了できる。scope 超過・予算超過・stale approval・未承認 external code・停止・再開の negative test を通す。Discord↔Notion の同期と正本の整合性を検証する。Production 実行が別途未承認なら dry-run／隔離テストで証明し、実機本番を成功と偽らない。

---

# ユーザー決定（2026-10-11）

推奨案をすべて採用。研究目的は親タスクのページ（見出しで構造化）、起動は Run Autopilot、子タスクは承認後に作成、自律実行上限の既定値（再計画 5 サイクル・1 サイクル追加 3 件・再試行 2 回・進展なし 2 サイクルで停止・既定予算）は承認対象として提示、Discord に停止／再開ボタン（停止は新規投入停止・実行中ジョブは完了まで）、変更案は親ページへの版付き記録＋Discord 承認、通知は承認依頼・マイルストーン・成果・異常のみ、E2E は 10A を Phase A の研究目的（計画のみ）、10B をユーザー作成の INU-292（[E2E] H₂ RHF–FCI 結合解離曲線）で実施。LLM 上限は計画／再計画 1 回 $2。
