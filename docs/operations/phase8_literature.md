# Phase 8 Evidence-grounded Literature Research

仕様は `docs/phase_specs/phase8.md`。ユーザー決定（2026-10-11）：推奨案をすべて採用、連絡先 `takagi.r.f3bb@m.isct.ac.jp`、Notion Notes の分類「文献」はユーザーが作成済み。追加決定（同日）：CAPTCHA が出ない論文（購読論文を含む）は自動取得してよい。ただし 1 回の調査あたりの自動取得は 10 件まで。

## 構成（`src/research_autopilot/literature/`）

| モジュール | 役割 |
|---|---|
| `net.py` | 許可リスト方式の HTTPS クライアント（リダイレクト先も再検査、cookie・認証情報なし、mailto 付き、host ごとの間隔、サイズ上限、GitHub token は任意） |
| `search.py` | arXiv / Crossref / OpenAlex / Unpaywall / GitHub の検索、DOI・arXiv ID・正規化タイトルでの重複統合、preprint ↔ 出版版の関連付け。失敗したサービスは記録して続行 |
| `zotero/` | **PoC（`zotero-connector-poc` @ 58b3871、方式 C）を移植**。保存・判定・CAPTCHA 検出のロジックは検証済みのまま、設定だけ RA の環境変数に移した。保存先コレクションの事前確認、DOI 既存時は保存しない（重複防止） |
| `acquire.py` | 原文取得：Zotero 既存 PDF → Zotero Connector → OA 直接取得（非公開キャッシュ）→ 人への依頼。参照実装は commit 固定のスナップショット |
| `text.py` | pypdf によるページ単位の本文抽出（sha256 でキャッシュ）、実装ファイルの読み込み、引用照合 |
| `analysis.py` | LLM：資料選定・主張抽出・統合。外部テキストは信頼しないデータとして区切って渡す。引用の決定論的照合 |
| `researcher.py` | 全体の流れと段階記録・費用・失敗の保持 |
| `store.py` | Research Artifact の保存と Phase 7 Catalog 連携（リポジトリ `ra-literature`、組み込み Adapter）、再利用 |
| `notes.py` | Notion Research Notes（分類「文献」） |
| `integration.py` | Planner 連携（seed 抽出、Planner 向け文脈） |
| `cli.py` | `research-agent literature research|search|show|zotero check|zotero fetch-connector` |

## 根拠の扱い

- LLM が抽出した主張は提案にすぎない。**引用（quote）が保存済み原文に実在すると決定論的に確認できた主張だけ `verified`**。PDF はページ番号、実装は `path:line`、コミット SHA と PDF の sha256 を記録する。
- 照合は NFKC・合字・引用符の統一に加え、行末ハイフンを「分割語」「複合語」「ハイフン除去」の 3 通りで比較する（1 文に両方が混ざる PDF があるため）。12 文字未満の引用は根拠として扱わない。作り話の引用は一致しないことを確認済み。
- 原著論文の記述（origin=paper）と参照実装の選択（origin=implementation、kind=implementation_choice）を区別して保存する。
- 資料間・論文と実装間の矛盾は claim id 付きで保持する（存在しない id は除外）。
- 原文を取得できなかった資料、本文を抽出できなかった資料、言及のない論点は `unresolved` / `failures` に残す。
- Planner への文脈では VERIFIED / UNVERIFIED を明示し、「固定された科学仕様の一部ではない」と記す。Planner には仕様変更をせず `approvals_needed` に回すよう指示している。Notion の仕様本文は変更しない。

## 取得の方針

1. Zotero に同じ DOI（arXiv は `10.48550/arxiv.<id>`）の PDF があり、ファイルが実在すれば再利用（Zotero は読み取りのみ）。
2. Zotero Connector で保存（Zotero で `RA_ZOTERO_COLLECTION`、既定 `ResearchAutopilot` が選択されていなければ保存しない）。PDF は ZotMoov によって Drive 同期フォルダへ。**購読論文もここで保存する**（ブラウザでの通常の機関アクセス）。bot check / CAPTCHA が出たら回避せず `requires_human` とし、その調査では以後 Connector を使わない。
3. OA（arXiv / Unpaywall の OA 位置）は `~/.research-autopilot/literature/pdf/` に直接取得（非公開。Git・Notion・Discord には出さない）。
4. 以上が不可なら `requires_human`（人に Zotero への手動保存を依頼）として記録。CAPTCHA は検出して記録するだけで回避しない。
5. 参照実装は GitHub API で既定ブランチの commit SHA を取得し、README・LICENSE・ソース（1 ファイル 300 KB、合計 3 MB、80 ファイルまで）を `raw.githubusercontent.com/<repo>/<sha>/...` から保存する。
6. **取得上限**：Connector 保存・OA 取得・参照実装スナップショットの試行は 1 回の調査で合計 10 件まで（`RA_LITERATURE_MAX_RETRIEVALS` で下げられるが 10 を超えない）。上限到達後の資料は `requires_human`。Zotero 既存 PDF・catalog 済み資料の再利用は数えない。

Connector は内部 API 依存のため、PoC で検証した 5.0.217 を `~/.research-autopilot/zotero/connector` に固定して使う（`zotero fetch-connector` は更新時のみ。更新後は `zotero check` と E2E で回帰確認）。

## Research Artifact と再利用

- `~/.research-autopilot/literature/store/reports/<report_id>.json`（問い・資料・主張・矛盾・未解決・失敗・段階記録・費用）と `sources/<key>.json`（書誌・種別・版・取得方法・取得元 URL・取得日時・sha256）。
- この store を Catalog のリポジトリ `ra-literature` として登録し、組み込み Adapter `ra-literature@1.0.0`（RA 同梱、`ra-builtin` が有効化）で `literature-report` / `literature-source` を catalog 化する。
- 同じ問い（正規化した問い＋seed）の verified レポートが 30 日以内にあれば調査せず再利用する。資料は DOI / arXiv ID / repository が一致し、ファイルの sha256 が一致すれば再取得しない。

## Planner 連携

- タスク仕様に `## Literature` / `## Literature research` / `## 文献調査` があれば、その本文を問い（DOI・arXiv ID・GitHub URL は seed）として計画前に調査する。
- Planner の出力 `literature_requests`（任意）があれば最大 2 問を調査し、1 回だけ再計画する。
- handoff に `literature_reports`（report id）を記録。調査の失敗は計画を止めず、Planner への文脈（根拠不足）とイベントに残す。
- `RA_LITERATURE_CONTACT_EMAIL` が未設定なら Researcher は無効（要求は「未設定」として記録）。

## 受け入れ検証（2026-10-11 / Mac）

| 受け入れ条件 | 状態 |
|---|---|
| 研究タスクから原著論文・参照実装候補を発見 | ✅ 実データ：BOCS（arXiv:1806.08838v2）が arXiv / OpenAlex / Crossref の統合結果で首位、`baptistar/BOCS`（★105）を GitHub で発見 |
| OA PDF の自動取得 | ✅ テスト（OA 直接取得）。実データでは Connector 経由で取得 |
| PoC を使った Zotero Connector 保存 | ✅ Mac：BOCS を `ResearchAutopilot` コレクションに保存（11.6 秒、PDF＋同期フォルダ）、2 回目以降は既存 PDF を再利用。**Windows（Eagle）は未検証** |
| 購読論文の取得可否 | ✅ テスト：CAPTCHA なしなら Connector で保存（access=subscription）、CAPTCHA なら回避せず `requires_human` で以後 Connector 停止、1 回 10 件上限。**購読論文での実地検証は未実施** |
| PDF と書誌の対応付け | ✅ DOI / arXiv ID / Zotero item key / 取得元 URL / sha256 |
| 取得失敗・未対応条件の検出 | ✅ サービス障害・OA なし・Connector 不可・本文なしを記録（テスト）。実ページでの CAPTCHA は未遭遇 |
| 原文に基づく主張抽出・検証可能な出典 | ✅ 実データ：66 主張中 63 件を原文で照合（照合改善後の再計算では 66/66。保存済みレポートは不変のため 63 のまま） |
| 文献の記述と実装上の仮定の区別 | ✅ 実データ：実装の既定値・推定器・閾値などを `implementation_choice`（path:line）として分離、論文との不一致 5 件を検出 |
| DOI・URL・版・取得日時を含む Artifact | ✅ Catalog で verified、Notion Notes（分類「文献」）作成 |
| Artifact の再利用 | ✅ 同じ問いでは LLM・通信なしで再利用（テスト・実データ）、資料単位の再利用（テスト） |
| E2E（BOCS 1〜6） | ✅ 1〜5 を実データで実施。6 は実 Planner / Reviewer で、保存済みレポートを再利用し S1・S2 を引用した計画を生成。ただし試験用の簡素なタスク仕様に対し Reviewer が実験設計（予算・反復・標準誤差の扱い）を理由に 2 回差し戻し、計画は Blocked（文献連携ではなく計画内容の問題） |

実モデル費用：BOCS 調査 $0.52、Planner/Reviewer $0.32、ほかに途中で失敗した調査 1 回分（費用未記録、同程度と推定）。

## 既知の制約・未検証

- Eagle（Windows）での Connector 保存と Drive クラウド到着は未検証（PoC と同じ）。
- 購読論文：実際の出版社ページ（機関アクセス・CAPTCHA）での Connector 保存は未検証。
- pypdf は数式・表のレイアウトを失うことがある。数式の主張は引用が一致しにくく `unverified` になりやすい（安全側）。
- 選定は LLM。外れた場合は seed（DOI / arXiv / GitHub）を仕様に書くと確実。
- Connector 内部 API 依存（5.0.217 に固定）。
