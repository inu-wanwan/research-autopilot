# Phase 7B Autonomous Adapter Generation

仕様は `docs/phase_specs/phase7b.md`。Phase 7A の Catalog / Adapter lifecycle（`docs/operations/phase7a_catalog.md`）をそのまま使い、Catalog core に repository 固有ロジックは追加していない。

## 流れ

```text
detect → survey（read-only・上限付き）→ AI 提案（declarative 優先）→ generated として新 version を register
      → 自動検証（失敗なら問題点を返して最大 N 回再生成）→ 有効化ポリシー → 記録・通知
```

| モジュール | 役割 |
|---|---|
| `catalog/generation.py` | detect、generator（OpenAI Agents SDK、既存の単価・予算拒否を再利用）、自動検証、有効化判断、`GenerationRun` 記録、通知 |
| `catalog/survey.py` | 構造の要約（一般化した path shape と件数、JSON 構造サンプル、README / config 抜粋）。隠しディレクトリ・仮想環境・秘密らしい名前のファイルは開かない。約 9 万文字に制限 |
| `catalog/sandbox.py` | 生成 python Adapter の隔離実行と出力検証 |

## 検出（`detect`）

`unbound`（binding なし）、`no_active_adapter`、`scan_failed`（最新 scan にエラー）、`no_artifacts`（0 件）、`stale_artifacts`（scan で観測されなくなった artifact）で生成が必要と判定。`repository_unavailable` は対象外。`--force` で明示的な再生成も可能。

## 自動検証（`autovalidate`）

| gate | 内容 |
|---|---|
| definition_safety（declarative） | Phase 7A schema（repository 相対 pattern、`..`・絶対 path 禁止、固定 check 語彙、既知 validator のみ、未知キー禁止）、サイズ・rule 数・check 数の上限、各 rule に mandatory check 必須 |
| python_static_safety（python） | AST で import allowlist、`eval`/`exec`/`__import__`、書き込み mode の `open`、削除・移動・mkdir 等、dunder 属性を拒否、`discover` 定義必須 |
| sandbox_available（python） | OS sandbox が無いホストでは実行せず不合格 |
| discovery_errors / discovers_artifacts | 読み取りエラーなし、1 件以上 |
| deterministic | 2 回の独立 discovery の artifact id 集合が一致 |
| repository_unchanged | git HEAD + `status --porcelain`（非 git は path/size/mtime）を前後比較 |
| time_budget | 1 回 300 秒以内 |
| artifact_granularity | 50,000 artifact 以下（1 ファイル 1 artifact の過剰分割を拒否） |
| unique_identities / verifies_some_artifacts | 重複なし、実データで 1 件以上 verified |
| diagnostics | 不合格時、rule ごとの一致数・capture での脱落・欠落 field と JSON pointer の到達位置を generator に返す |

再生成時は、過去全試行の問題点（最新を先頭）と直前の提案本文を generator に渡す。

## 有効化ポリシー

- **generated declarative**：全 gate 合格かつ次を満たすときだけ `autopilot` が自動有効化し binding に追加。
  - 人間作成（manual）の active Adapter がない（ある場合は `awaiting_human`、自動で置き換えない）。
  - 現在 active な generated version が verified にしていた artifact をすべて verified にする（減るなら regression として `awaiting_human`、旧 version は active のまま）。
- **generated python**：sandbox で検証し `validated` のまま `awaiting_human`。`adapter activate --actor <人間>` のみ有効化可（`autopilot` は拒否）。有効化後も RA 本体では import せず、discovery は毎回 sandbox で実行し、member は親プロセスで再ハッシュする。
- 検証失敗の version は `rejected`。既存 active Adapter は変更されない。

### Artifact 再利用との独立（trust policy）

Adapter の有効化と Artifact の科学的妥当性は独立した判断（仕様 §3）。実モデル試行で、自動有効化された generated Adapter が手作り Adapter より弱い check（base cache との照合や収束確認なし）で旧 run を verified にした例が確認されたため（後述）、次を既定とした。

- 人間がレビューしていない generated Adapter（`autopilot` が有効化、`endorsed_by` なし）だけが verified にした artifact は、Planner / Executor の解決で再利用しない。発見・検索・カタログ化は行う。
- 再利用するには人間が `research-agent catalog adapter endorse <id> --actor <you>` で check をレビュー済みと記録するか、タスク要求に `trust=any` を明示する（人間が書く仕様側の判断）。

## sandbox（`catalog/sandbox.py`）

- 別 interpreter（`-I -S`、site-packages なし）、空の作業ディレクトリ、最小環境変数、CPU 時間・出力サイズ・fd 数の rlimit、wall timeout で process group ごと kill。
- macOS：`sandbox-exec`。network・fork・exec（interpreter 以外）・作業ディレクトリ以外への書き込みを拒否。読み取りは deny-by-default で、repository・作業ディレクトリ・interpreter・OS ライブラリ（と `/` の一覧）だけ許可。
- Linux：`bwrap`（`--unshare-all`、repository は read-only bind）。**このホストに bwrap がなく未検証**。
- どちらも無いホストでは生成 python Adapter を実行しない。
- 出力は `{"groups": [...]}` 以外を拒否。member は repository 内の通常ファイルのみ（絶対 path・`..`・symlink を拒否）、hash は親が再計算。

Mac で確認済み：repository 書き込み、repository 外（作業領域の別ファイル、`/etc/passwd`、`~/.../.env`）の読み取り、network、subprocess、fork がすべて拒否され、repository の読み取りと標準ライブラリ import は可能。

## 運用（CLI）

```bash
research-agent catalog detect                         # 全登録 repository の要否
research-agent catalog generate <repo> --focus "<what matters>" --budget-usd 1.0
research-agent catalog generations <repo>             # 試行・問題点・費用・結果の履歴
research-agent catalog adapter activate <id> --actor <you>   # python / awaiting_human の有効化
research-agent catalog adapter endorse <id> --actor <you>    # generated check のレビュー記録
```

環境変数：`RA_ADAPTER_GENERATOR_MODEL`（既定 `RA_REVIEWER_MODEL`、`RA_MODEL_PRICING` に単価必須）、`RA_ADAPTER_GENERATION_BUDGET_USD`（既定 1.0）。通知は `awaiting_human` → approvals、`activated` → logs、`rejected`/`failed` → errors（Discord ボタンは付かない）。

## 実モデル検証（2026-10-11 / Mac、作業用 SQLite、モデル `gpt-5.6-sol`）

合計費用は約 $2.13（合成 $0.51、実 repository $1.63）。研究 repository は全試行の前後で `git status` clean。

| repository | 構造 | 結果 |
|---|---|---|
| toy-casci（合成） | 条件結果 JSON + subset cache、2 run | 改善後 2 試行目で自動有効化、6 artifact verified（$0.049） |
| toy-shards（合成） | 1 artifact = 複数 shard ファイル | 改善後 1 試行目で自動有効化、6 verified（$0.020） |
| toy-runs（合成） | 実験 × seed の manifest / metrics | 改善後 1 試行目で自動有効化、8 verified（$0.024） |
| active_space_research（実） | Phase 4 orbital 結果 + base cache | 改善後 3 試行目で自動有効化（$0.49）。survey 改善後の再試行（新 DB）で 1 試行目に自動有効化（$0.17） |

初回の実モデル試行では全提案が gate で拒否された（`captures` を rename と誤解、`item` の空 pointer、capture と field の名前衝突、存在しない pointer）。拒否自体は正しく、何も有効化されなかった。これを受けて語彙表・schema 説明・累積 feedback・pointer ヒント付き診断・数値として等しい capture の比較・大きな JSON 系列の survey サンプルを追加した。

実 repository の生成 Adapter について：

- 1 本目（@0.9.0）は結果 JSON の構造整合（geometry・k・件数・有限値）を検査したが、base cache との照合と収束確認がなく、手作り Adapter が inconsistent とする旧 run（6c17c30・3a61c10）も含む 204 件を verified にした。trust policy 導入の根拠。
- 2 本目（新 DB、@0.1.0）は結果と cache entry の 2 rule を作り、cache 側で収束・形式 version・軌道並びを検査（旧形式 55,489 件は verified にならない）。ただし cache を subset ごとの artifact とし 165,031 artifact になったため、その後 `artifact_granularity` gate（50,000）を追加した。**この gate は当該試行の後に追加したもので、@0.1.0 は現行 gate では不合格になる。** 結果↔cache の cross_reference は generator が根拠不足として未作成（unresolved に記録）。
- いずれも trust policy により、人間の endorse か `trust=any` がない限り Planner / Executor には渡らない。科学的再利用には手作り Adapter（`active-space-phase4-orbital`）を使う。

## 既知の制約

- Linux（Eagle）の bwrap sandbox は未検証。bwrap が無ければ python Adapter は検証できない（安全側）。
- generator が作る check の科学的十分性は自動では判断できない（trust policy で再利用を止める）。
- survey は約 9 万文字に要約するため、大規模 repository では focus の指定が品質に影響する。
- autopilot poll からの自動起動はしていない（CLI / 関数呼び出し）。
