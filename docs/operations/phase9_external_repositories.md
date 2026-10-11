# Phase 9 Trusted External Repository Onboarding

仕様は `docs/phase_specs/phase9.md`。ユーザー決定（2026-10-11）：推奨案をすべて採用、承認者は Discord の既存承認者（ユーザー本人のみ）。

## 決定事項

| 項目 | 決定 |
|---|---|
| ライセンス | MIT / BSD / Apache-2.0 / ISC / MPL など許容型は自動。GPL / LGPL / AGPL は「研究室内で配布しない利用」なら自動。研究リポジトリへの取り込み・配布、ライセンスなし・独自・矛盾は人間（Discord） |
| 隔離 | Docker。依存は PyPI の binary wheel のみ（`pip download --only-binary=:all:`、ビルドスクリプトは実行しない）、ハッシュ固定で `--network none` ビルド。検証実行はネットワークなし・ホスト環境変数なし・秘密情報なし・ホーム／研究データのマウントなし・読み取り専用ルート・全 capability 削除・非特権ユーザー・CPU / メモリ / PIDs / 時間 / tmpfs 容量制限 |
| 動作テスト | テストスイートがあれば実行、各モジュールの import 確認、LLM が提案する縮小版 smoke 実行（最大 3）。失敗は stderr 付きで計画に戻し、依存のピン留め・Python 版・wrapper 修正のみで最大 3 試行（コードは変更しない）。全試行を `attempts` に保存。1 回の導入あたり LLM $1 |
| 研究用実行 | Phase 4 Runner から、承認済みイメージのコンテナで実行（外部コードは `/ext/<mount_as>` に読み取り専用、研究側はジョブ専用チェックアウト `/work`）。実行のたびに承認内容と照合 |
| 変更の追跡 | 外部リポジトリは研究リポジトリにコピーしない。研究用の変更は研究リポジトリの `*.patch` として `external_code.patches` に列挙。パッチはハッシュ単位で承認される |
| 起動 | Phase 8 の参照実装・タスク仕様で名指しされた GitHub リポジトリは計画時に Registry 登録まで自動。実行承認は実験が `external_code` で使うときに要求 |
| 検証資源 | CPU 2・メモリ 4 GB・1 実行 20 分・tmpfs 2 GB（`RA_EXTERNAL_SANDBOX_*`） |

## 構成（`src/research_autopilot/external/`）

| モジュール | 役割 |
|---|---|
| `github.py` | 識別（URL・提供元・対応論文・fork・commit 固定）と提供元分類（official：論文本文がリポジトリを示す／author：所有者が著者／third_party／unknown、根拠と不確実性を記録）。commit の tar.gz を認証なしで取得し `data` フィルタで展開、読み取り専用化、全ファイルを Git blob SHA-1 で commit のツリーと照合 |
| `license.py` | GitHub API・LICENSE ファイル・ソースヘッダからライセンスを決定論的に判定し、利用・改変・再配布条件を記録、方針に従って auto_ok / needs_human / incompatible |
| `deps.py` | 依存ファイル（`setup.py` は ast で読むだけ、実行しない）と import の静的調査、PYTHONPATH、テスト・エントリポイント、扱えない実行環境（MATLAB 等） |
| `scan.py` | 静的安全性スキャン（資格情報の読み取り・マイナー・`curl | sh`・難読化 exec・リバースシェルは critical＝不合格。subprocess・ネットワーク・eval・pickle・同梱バイナリ等は warning として承認依頼に表示） |
| `sandbox.py` | Docker 操作（wheel 取得、オフラインビルド、隔離検証実行、承認済み研究実行の argv 生成）。docker CLI にも API キー等を渡さない |
| `verify.py` | 検証パイプラインと LLM テスト計画（README 等は信頼しないデータとして区切る） |
| `registry.py` | DB（`external_entries` / `external_grants` / `external_decisions` / `external_executions`）を正とし、Phase 7 Catalog（リポジトリ `ra-external`、組み込み Adapter）に provenance・検証結果・承認・利用履歴を反映 |
| `grants.py` | 実行承認（Discord）と実行時ゲート、パッチ適用、実行記録 |
| `service.py` | 導入パイプライン（識別→取得→ライセンス→検証→自動登録）、更新の再検証、パッチ済みコードの再検証 |
| `integration.py` | Phase 8→9、Planner / Implementer への提示、Phase 4 のジョブ準備、Phase 5 poller |
| `cli.py` | `research-agent external onboard|list|show|history|request-grant|decisions|revoke|run`（通常運用では不要） |

## 状態の区別

- Registry entry（リポジトリ × commit）：`fetched → verifying → registered`、または `failed` / `needs_human`、新しい検証済み commit に置き換わると `superseded`。**registered は実行許可ではない**。
- Execution grant（人間の承認）：対象 entry・commit・ソースツリー hash・wheel ロック hash・イメージ ID と、用途（カテゴリ）・研究リポジトリ・ファイルアクセス・ネットワーク・CPU / メモリ / 時間・累積 CPU 時間・承認済みパッチ hash に結び付く。`active` / `revoked` / `expired`。
- Execution record：実際の研究実行（grant・commit・イメージ・パッチ・実コード hash・タスク・実験・終了コード・CPU 時間）。

## 実行時の照合（毎回）

entry が検証済みで、手元のソースがツリー hash と一致し、イメージが存在すること。さらに次をすべて満たす active grant があること：commit・依存（ロック）・イメージが同一、用途が承認カテゴリ内、研究リポジトリが承認範囲内、ネットワーク・書き込み・追加データパスが承認以下、CPU / メモリ / 時間が上限以下、累積 CPU 時間が予算内、パッチが承認済み hash のみ、期限内。

満たさない場合は実行せず、既存 grant と要求を包含する範囲で新しい承認依頼を作る（パッチがあれば、適用可否と import 確認をサンドボックスで再検証してから依頼する）。人が却下・修正依頼した同一内容は自動で再依頼しない。

Phase 5 の他の承認（委任・予算・計画など）は従来どおり必要。外部コードの承認はそれを置き換えない。

## Phase 連携

- Phase 2：Phase 8 レポートの参照実装と、仕様に書かれた `github.com/<owner>/<name>` を Registry に自動登録し、Planner に状態（登録済み・承認なし）を示す。handoff の `external_implementations` に記録。費用は `external_cost_usd` として chain 予算に計上。
- Phase 3：Implementer は外部コードをコピーせず `external_code` を宣言し、研究リポジトリ側のスクリプトから import する。`external_code` を使う実験は環境変数（資格情報）・setup 手順を持てない。
- Phase 4：`external_code` のある実験はゲートを通ったときだけ承認済みコンテナで実行。承認がなければ Blocked と承認依頼。結果の `environment` に外部コードの provenance（entry・commit・コード hash・イメージ・ロック・grant・パッチ）を残す。
- Phase 5：poller が Discord の判断を適用し、待っていた run を再開（CLI 操作は不要）。登録済みリポジトリは 24 時間ごとに既定ブランチの更新を確認し、新しい commit は改めて検証（合格するまで旧 entry が current のまま）。

## 受け入れ検証（2026-10-11 / Mac）

E2E は BOCS（`baptistar/BOCS`）で実施。

| 工程 | 結果 |
|---|---|
| 1 リポジトリと論文の特定 | ✅ 実データ：Phase 8 レポートから論文 arXiv:1806.08838 と対応付け、論文本文に `github.com/baptistar/BOCS` があるため official と判定。commit `fef0d4e` に固定 |
| 2 ライセンス調査 | ✅ 実データ：GitHub API・LICENSE.md・ソースヘッダ 9 件がすべて GPL-3.0、研究室内利用として自動許可。初回は GPL 本文 13 条の Affero への言及を AGPL と誤判定→タイトル優先に修正し、保留中の人間確認は自動で取り下げ（Discord 未送信） |
| 3 固定 commit での隔離取得 | ✅ 実データ：103 ファイル全てが Git blob と一致、読み取り専用 |
| 4 依存・安全性・動作検証 | ✅ 実データ：依存 cvxpy / matplotlib / numpy / cvxopt、jar 同梱等の warning のみ。NumPy 2 では配列代入エラー（2018 年のコード）→ 2 回目で `numpy<2`・Python 3.11 にピン留め、3 回目で wrapper の誤った期待値を修正し、SA と SDP の両方の縮小実行が成功。MATLAB 53 ファイルは実行不可として未確認事項に記録。LLM 費用計 約 $0.22（3 回の導入試行の合計） |
| 5 Registry への自動登録 | ✅ 実データ：`ext-baptistar-bocs@fef0d4e34e37` を registered、Catalog に反映 |
| 6 未承認での通常実行の拒否 | ✅ 実データ：`external run` は「人間の初回承認が必要」で拒否、承認依頼を Discord 承認チャンネルへ送信 |
| 7 人間による初回実行承認 | ✅ 実データ：ユーザーが Discord で承認（`discord:818844326578356275`、許可ユーザー・hash・期限を再検証）→ grant 作成 |
| 8 承認範囲内での自動実行・再利用 | ✅ 実データ：承認済みコンテナ（ネットワークなし）で BOCS-SA baseline を実行、別タスク（INU-E2E-A / B）から追加承認なしで再利用 |
| 9 変更時の再承認要求 | ✅ 実データ：CPU 1→2、研究パッチ（サンドボックスで import 再検証）、旧 commit `918133f`（独立に再検証・登録、current は `fef0d4e` のまま）の 3 件とも拒否され再承認依頼。ユーザーが 3 件とも承認→それぞれ別 grant で実行 |
| 10 provenance・承認・実行履歴 | ✅ 実データ：entry 履歴、判断 4 件、grant 4 件、実行 5 件（タスク・grant・commit・実コード hash・パッチ hash・CPU 時間）を DB と Catalog（`ra-external`）で追跡 |

却下の経路（却下の記録・同一内容を再依頼しない）は実データでは未実施（ユーザーは 3 件とも承認）。オフラインテストで確認済み。

その他（テスト 39 件、すべてオフライン：模擬 GitHub・模擬 Docker）：改ざんアーカイブは不合格、critical 検出は実行前に不合格、テスト失敗は登録されない、更新失敗は旧 entry を置き換えない、登録だけでは実行不可、承認後は範囲内で自動実行・別タスクから再利用、範囲外（CPU・ネットワーク・用途・時間・研究リポジトリ・データパス・累積予算・未承認パッチ）は再承認、commit 変更は再検証＋再承認・旧 commit 固定は継続可、手元ソース改変は実行拒否、承認前の entry 変更で承認は stale、却下は再依頼しない、未許可の Discord ユーザーは承認不可、取り消し、Phase 4 / Phase 5 / Phase 2 の連携。

## 既知の制約・未検証

- Docker Desktop（Mac, arm64）でのみ検証。Eagle（Linux）での動作は未検証。
- 静的スキャンとサンドボックス実行は悪意がないことを証明しない（検証結果の未確認事項に常に記載）。
- 依存取得用コンテナは pip のみを実行するがネットワーク自体は PyPI 以外にも開いている（取得先は `--index-url https://pypi.org/simple` に固定）。
- Phase 4 のローカル実行（`external_code` を使わない通常の研究ジョブ）はホスト上で動くため、研究コードが外部コードの保管場所を絶対パスで読む経路を技術的には遮断していない（Implementer の規則と Reviewer で防ぐ）。
- sdist しかない依存、C 拡張のビルドが必要な依存、MATLAB / Julia / CUDA は扱えない（未確認事項として記録）。
- 外部コードの承認は Discord 経由のみ（`RA_APPROVAL_MODE` の legacy linear モードには対応しない）。
