# Phase 2 事前設定（Notion / Linear / OpenAI）

Phase 2（Task Manager / Orchestrator）の実装を始める前に必要な、外部サービスの認証情報・権限・接続確認の記録。
**この文書に秘密の値は書かない。** 状態は SET / UNSET で表す。

- 実施日：2026-10-10
- 実施者：ユーザー（発行・権限設定・課金）、Claude Code（調査・接続確認）
- 確認した環境：MacBook（macOS arm64）のリポジトリ直下の `.env`。研究室サーバーでの確認はまだ（§4）。

## 1. 環境変数

| 変数 | 種別 | 内容 | Mac `.env` | サーバー `.env` |
|---|---|---|---|---|
| `NOTION_API_TOKEN` | 秘密 | Notion internal integration「Research Autopilot Integration」の secret | SET | 未設定（§4） |
| `NOTION_TASKS_DATA_SOURCE_ID` | 非秘密 | Research Tasks の data source ID `3b469893-cf8f-802d-9f86-000bb4bbb21a` | SET | 未設定 |
| `LINEAR_API_KEY` | 秘密 | Linear personal API key | SET | 未設定 |
| `LINEAR_PROJECT_ID` | 非秘密 | Linear project「Research」の UUID `484c0d7b-f027-4a08-b433-6e9984b50436` | SET | 未設定 |
| `OPENAI_API_KEY` | 秘密 | OpenAI API key | SET | 未設定 |

参考（非秘密）：

- Notion Research Tasks database ID：`3b469893-cf8f-80f2-bc05-e8950262e0a0`（data source は1つ、名前「Tasks」）
- Linear：組織 `inu-wanwan`、チーム `INU`、project「Research」（slug `research-a4a57333034c`）。指示にあった識別子 `P-INU-1` では見つからず、実際の project はこの「Research」だった。
- Phase 2 の仕様：Notion ページ `3f469893cf8f817f9f7ef768fd99c9d3`、Linear issue `INU-265`（2026-10-10 時点で In Progress）

変数名は、Phase 1 の `.env` 方式（Git 管理外、`.env.example` に名前だけ）にそろえた。`OPENAI_API_KEY` は OpenAI SDK が標準で読む名前。

## 2. 人間が行った設定

| 作業 | 状態 | 確認方法 |
|---|---|---|
| Notion internal integration の作成、Research Tasks への接続 | 完了 | API で `users/me` と database の取得に成功 |
| Linear personal API key の発行 | 完了 | API で viewer を取得（admin ユーザー） |
| OpenAI API key の発行、クレジットの追加 | 完了 | 最小の Responses 呼び出しに成功 |
| Linear のテスト用 issue `INU-270`（`[TEST] Research Autopilot permission check`、project Research）の作成 | 完了 | 下記の書き込み確認に使用 |

推奨（まだ確認していない）：Notion integration の capabilities を「Read content のみ」にする。OpenAI の Limits で月の予算とアラートを設定する。Auto recharge は OFF のままにする。

## 3. 接続確認の結果（2026-10-10、MacBook）

| サービス | 結果 | 確認内容 |
|---|---|---|
| Notion | **PASS** | 認証成功（integration「Research Autopilot Integration」）。`GET /databases/{id}`（Notion-Version `2025-09-03`）の data source が `.env` の値と一致。data source のプロパティを11件取得：Name(title), Status(status), Linear Issue ID(rich_text), Linear URL(url), Linear(button), Pending(checkbox), Due(date), 分類(select), 担当者(people), 親タスク(relation), サブタスク(relation)。query で行を取得（`has_more: true`）。Phase 2 仕様ページの本文（19 ブロック）を読めた |
| Linear（読み取り） | **PASS** | 認証成功。project「Research」を UUID で取得。issue INU-264〜268 を取得。INU チームのワークフロー状態：Backlog / Todo / In Progress / Done / Canceled / Duplicate |
| Linear（Status の書き込み） | **PASS** | ユーザーの承認のうえ、テスト用 issue INU-270 だけを Todo → In Progress → Todo と変更し、2回とも `success: true`、最後に Todo に戻ったことを確認。研究用の issue は変更していない。INU-270 には対応する Notion タスクがないことをユーザーが確認済み |
| OpenAI | **PASS** | 認証成功。`GET /models` で 135 モデルを取得（gpt-5 / 5.1 / 5.2 / 5.4 / 5.5 系、gpt-4.1 系、o3 など）。`gpt-5-mini` への Responses 呼び出しが `completed`（30 トークン）。最初はクレジット不足で `429 insufficient_quota` だったが、ユーザーがクレジットを追加して解消 |

確認に使ったのは scratchpad の一時スクリプトで、リポジトリには入れていない（Phase 2 の connector 実装で正式に作る）。読み取りは GET / query のみで、書き込みは INU-270 の状態変更の2回だけ。

## 4. 本番（研究室サーバー）への秘密情報の受け渡し方式

- **置き場所**：研究室サーバーのリポジトリ直下の `.env`（`chmod 600`、Git 管理外）。Mac の値を使い回すか、サーバー用に別の key を発行するかはユーザーが決める（別に発行すると、片方を失効しても他方に影響しない）。
- **受け渡し**：Phase 1 と同じく、Compose が `.env` を読み込んで `prefect-worker` の `environment` に `${VAR:-}` で渡す。Phase 2 の flow は worker のサブプロセスで動き、環境変数を引き継ぐ。
- **まだ行っていないこと**：`infra/compose.yaml` の `prefect-worker.environment` に5つの変数を追加する変更と、worker の再作成（`ra-compose up -d prefect-worker`）。Phase 2 のコードが使い始める時点で行う。**worker の再作成は実行中の run に影響するので、行う前にユーザーの承認をもらう。**
- Prefect の Secret block や外部のシークレット管理は使わない（Phase 1 の方式にそろえ、管理の仕組みを二重にしない）。
- 開発は macOS、本番は Linux サーバーで、Windows / WSL2 は使っていない。

## 5. Phase 2 の実装で決めること（事前設定の障害ではない）

- 使うモデル：仕様にモデルの指定はない。この key で gpt-5 系が使える。
- 仕様にある「Notion / Linear sandbox による統合テスト」用の sandbox（テスト用 database / team など）はまだ用意していない。
- OpenAI の予算上限は、ユーザーが管理画面で設定する（§2 の推奨）。
- テスト用 issue INU-270 は、今後の書き込みテストに使い回すか、Canceled にするかを決める。
