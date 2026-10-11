# Phase 7A Research Artifact Catalog

仕様は `docs/phase_specs/phase7a.md`（親：`phase7.md`）。本書は実装した設計・運用手順・実データ検証の記録。

## 構成と責務

| 層 | 場所 | 役割 |
|---|---|---|
| Catalog core | `src/research_autopilot/catalog/{models,service}.py` | identity / metadata / provenance / content hash / validation status / 環境別 location の管理、scan、query、resolve。Repository 固有の path・命名・schema を持たない |
| Adapter 管理 | `catalog/adapters.py` | register → validate → activate の分離、version 不変、履歴、repository binding |
| Declarative engine | `catalog/declarative.py` | データのみの Adapter 定義を解釈（path pattern、JSON pointer、grouping、固定語彙の check） |
| 名前付き validator | `catalog/validators.py` | Adapter が名前で参照できる信頼済み RA コード（分野固有の検証を追加する場所） |
| 連携 | `catalog/integration.py` | Phase 2 / 3 / 4 への受け渡し |
| CLI | `catalog/cli.py` | `research-agent catalog ...` |
| Repository 固有定義 | `config/catalog/adapters/*.json` | 例：`active-space-phase4-orbital.json` |

PostgreSQL（既存 `research_autopilot` DB）に `catalog_adapters`、`catalog_bindings`、`catalog_artifacts`、`catalog_scans` を追加。record は JSON、index 列は lookup 境界のみ。

## Adapter

- 種別：`declarative`（データのみ。プログラム実行・import・絶対 path・`..`・symlink 追跡が不可能）と `python`（`research_autopilot.catalog.builtin.*` の信頼済みモジュールのみ import 可）。
- 生成元：`manual` / `generated`（Phase 7B）。`generated` の python Adapter は ResearchAutopilot 本体で決して import しない。
- 状態：`registered`（保存のみ）→ `validated`（静的検査＋対象 repository への read-only dry discovery に合格）→ `active`（discovery に使用）。不合格は `rejected`、置換・停止は `retired`。同名の新 version を active にすると旧 version は自動で retired。
- 同じ `name@version` に異なる内容は登録できない（version を上げる）。
- 有効化方針 `adapters.requires_human()`：`generated` かつ `declarative` 以外は人間の明示的有効化が必要（`--actor autopilot` は拒否）。Phase 7B は generated declarative の自動有効化をこの hook で扱う。
- binding は repository → Adapter **名**。active な version だけが使われる。複数 repository で同じ Adapter を共有できる。

### Declarative 定義

`artifacts[]` の各 rule：

- `pattern`：repository 相対。`{name}` は1セグメントを capture（`prefix{name}suffix` も可）、`*` / `*.ext` は1セグメント。
- `fields`：JSON pointer（`prefix`＝文字列先頭 N 文字、`item`＝リスト各要素の pointer、`transform`）。
- `identity`：研究条件。`provenance`：由来（run、config hash 等）。identity と provenance の組ごとに1 artifact。同じ identity で provenance が違う artifact は scan では統合せず、解決時に ambiguous とする。
- `metadata` / `derive`：付帯情報と集計（min / argmin 等）。
- `checks`：`equals`、`one_of`、`uniform`、`equals_field`、`finite`、`distinct`、`sorted_unique`、`count`（固定数 or C(n,k)）、`items_equal` / `items_finite` / `items_subset` / `items_min_equals`（リスト要素）、`cross_reference`（同一 repository 内の別ファイル群と照合。pattern placeholder は member field、`target_match` / `target_equals` で照合先を限定）、`validator`（名前付き validator）。

## Validation status と再利用規則

| status | 意味 | 自律的再利用 |
|---|---|---|
| verified | Adapter が宣言した mandatory check がすべて合格 | 可（下記条件付き） |
| unverified | mandatory check が宣言されていない | 不可 |
| inconsistent | mandatory check が不合格 | 不可 |
| stale | 検証後に内容が変化 / scan で観測されなくなった | 不可 |

解決（`service.resolve`）は fail-closed：該当なし、要求値の欠落、組み合わせ欠落、`expect` 件数不一致、同一 identity の複数 artifact（**ambiguous**、status に関係なく）、verified 以外、当該環境に location がない、当該環境で未検証の content、root にアクセス不能、使用直前の再ハッシュ不一致（→ stale に更新）をすべて problem として返し、problem があれば Planner / Executor は進まない。

Adapter の有効化と Artifact の科学的妥当性は独立：active な Adapter が見つけた artifact でも check 不合格なら再利用しない。Phase 7B 以降、人間がレビューしていない generated Adapter だけが verified にした artifact も既定では再利用しない（`adapter endorse` または要求に `trust=any`。`docs/operations/phase7b_adapter_generation.md`）。

## 実行環境

- `RA_CATALOG_ENVIRONMENT`（既定 `local`）が現在の環境名。registry の `path` はこの環境での root。
- 他環境の root は registry の `locations = { eagle = "~/..." }`。artifact は環境ごとに `ArtifactLocation`（root、相対 path、その環境で観測した content hash）を持ち、同じ bytes なら同じ artifact id に location が追加される。location は **その環境で scan したときだけ** 付く（未検証の他環境 path は使わない）。

## Planner / Executor 連携

タスクの Notion「Input artifacts」section に構造化行を書く（散文行は従来どおり Planner への文脈）：

```text
- catalog: kind=orbital-casci-exhaustive-condition repository=active-space-research molecule=N2,BeO,NH3 k=2..10 git_commit=6c0d1dd expect=81
```

- 値は `,` 区切り、整数範囲 `a..b`。filter は identity → metadata → provenance の順に照合し、7文字以上の文字列は前方一致可（commit / hash の短縮形）。
- Phase 2：決定論的 gate の直後、Planner 呼び出しの前に解決。problem があれば `DATA_INCONSISTENCY` で BLOCKED（LLM 費用なし）。成功時は解決要約を Planner 入力に追記し、`ExecutionHandoff.catalog_resolution` に凍結（Notion 仕様本文は変更しない）。
- Phase 3：`ImplementationTask.catalog_artifacts` として read-only 入力をプロンプトに提示（変更・再生成禁止を規則に明記）。
- Phase 4：job 投入直前に同じ要求を再解決・再ハッシュし、計画時と artifact 集合・hash が一致する場合のみ `<job>/catalog_inputs.json` を書き `RA_CATALOG_INPUTS` で渡す。不一致は job を開始せず Block。

## 運用手順（CLI）

```bash
research-agent catalog adapter register config/catalog/adapters/active-space-phase4-orbital.json --actor <you>
research-agent catalog adapter validate active-space-phase4-orbital@1.0.0 --repository active-space-research --actor <you>
research-agent catalog adapter activate active-space-phase4-orbital@1.0.0 --actor <you>   # 人間の明示操作
research-agent catalog bind active-space-research active-space-phase4-orbital --actor <you>
research-agent catalog scan active-space-research          # read-only。研究 repo には書かない
research-agent catalog list --summary
research-agent catalog resolve "catalog: kind=... git_commit=6c0d1dd expect=81"
```

## 実データ検証（2026-10-11 / Mac、作業用 SQLite）

対象：`active_space_research` main `b53f807`（作業ツリー clean、検証後も clean）。本番 PostgreSQL には登録していない。

- Adapter `active-space-phase4-orbital@1.0.0`：validate で dry discovery 204 件、active 化は検証用 actor `claude-verification`（本番 DB での有効化は人間が行う）。
- scan（約27秒）：204 artifact を発見。`supervisor_6c0d1dd` の 81 件が verified、`supervisor_6c17c30` の 81 件と `supervisor_3a61c10` の 42 件が inconsistent。
  - 6c0d1dd：81 条件の `full_evaluations` 計 52,312 件が `main_6c0d1dd/cache` の同一 config hash・正式 solver namespace（`pyscf-casci-sector-sweep-singlet-adaptive-davidson-v2`）・orbital cache v1 の entry と 1e-9 Ha 以内で一致、最小値＝`full_optimum`、候補 MO 内、全 converged、件数＝`enumerated_full_count`、commit・geometry・k の整合。
  - 6c17c30：`solver_metadata.converged` 欠落、照合先に正式 namespace の cache なし。3a61c10：同 commit の base cache が存在しない。
- 解決：commit 未指定の 81 条件要求は 81 件すべて ambiguous で 0 件返却（exit 1）。`git_commit=6c0d1dd` 指定で 81 件（N2/BeO/NH3 × 研究 repo `configs/phase4_orbital_main.yaml` の代表 3 geometry × k=2..10）を解決、使用直前の再ハッシュも合格。
- どの run を正とするかは研究 repo の記録（`docs/decisions.md` D-054 等）に基づく人間の判断であり、Catalog は推測しない（要求側で pin する）。

## 既知の制約

- scan は毎回全件を読み直す（増分 scan なし）。この repository で約27秒。
- 多数ファイルからなる artifact は `members_digest` を DB に保持するため record が大きくなる。
- `catalog.builtin` に python Adapter はまだない（declarative で足りた）。
- Eagle / TSUBAME での scan・解決は未検証。
- Adapter 定義の check 選択（許容誤差 1e-9 Ha、`working_tree_status` の許容値 `clean` / `clean_platform_line_endings_ignored`）は研究 repo の設定・記録に基づく作成者判断。本番 DB で有効化する前に人間がレビューする。
