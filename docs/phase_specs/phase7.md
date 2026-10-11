# Phase 7 — Research Artifact / Dataset Catalog

（2026-10-11 ユーザー提供。Phase 7A は `phase7a.md`、Phase 7B は `phase7b.md`。）

## 1. 目的

ResearchAutopilot が、登録済みの研究 Repository に蓄積された dataset、cache、実験結果、manifest などの研究成果物（Artifact）を、provenance を保持しながら自律的に発見・検証・再利用できるようにする。

研究タスクごとに人間がデータの保存場所や形式を指定する負担を減らし、必要な Artifact を研究上の要求条件に基づいて取得できる状態を目指す。

## 2. 要求仕様

- `research_repo.toml` に登録された Repository を対象とする。
- Repository ごとに、そのデータ構造を解釈する Adapter を対応付ける。
- Artifact Catalog 本体は、特定の Repository、ディレクトリ構造、科学分野に依存しない。
- Artifact の識別情報、metadata、provenance、整合性、保存場所を管理する。
- 研究タスクの要求条件に合致する Artifact を検索・取得できる。
- 科学的整合性が確認されていない Artifact を、検証済みとして自律的に再利用しない。
- Mac、Eagle など、実行環境ごとに異なるデータ保存場所を扱える。
- 未対応 Repository については、AI が Adapter を生成できる仕組みを提供する。
- Adapter の生成・検証・有効化において、定められた安全性と承認条件を遵守する。
- 既存の Planner / Executor と連携し、研究タスクの実行時に利用できる。

## 3. 実装フェーズ

### Phase 7A — Artifact Catalog Core / Repository Adapter

Artifact の登録・検索・検証・再利用に必要な基盤を構築する。

Repository ごとの Adapter を手動で用意・登録できるようにし、将来的な自動生成・有効化にも対応可能な設計とする。

### Phase 7B — Autonomous Adapter Generation

未対応 Repository に対し、AI が Adapter を生成・検証し、所定の承認条件に従って有効化できる仕組みを構築する。

Phase 7A で実装した Artifact Catalog と Adapter 管理機構を再利用する。

## 4. 共通制約

- 既存の研究データを無断で変更・削除・再生成しない。
- Artifact の由来や科学的整合性が不明な場合、推測で検証済みとしない。
- 既存の研究タスクの科学仕様を変更しない。
- Notion Research Tasks / Notes、Linear、PostgreSQL の既存 SSOT を維持する。
- 人間承認を必要とする既存の操作を迂回しない。
- Eagle / TSUBAME への本番 deployment は別途承認を必要とする。

## 5. 完了条件

- 登録済み Repository の Artifact を自律的に発見・検証・再利用できる。
- Repository 固有の構造が Artifact Catalog 本体にハードコードされていない。
- 未対応 Repository に対する Adapter 生成・導入の仕組みが動作する。
- 検証・承認条件を満たさない Adapter や Artifact が不適切に利用されない。
- Phase 7A および 7B の個別完了条件を満たす。
