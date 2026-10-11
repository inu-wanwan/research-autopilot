# Phase 7A — Artifact Catalog Core / Repository Adapter

（2026-10-11 ユーザー提供。親仕様は `phase7.md`。）

## 1. 目的

ResearchAutopilot が、登録済みの研究 Repository から Artifact を発見・検証・再利用するための基盤を構築する。

また、後続の Phase 7B で Adapter を自動生成できるよう、Repository と Adapter の関係および Adapter のライフサイクルを確立する。

## 2. 要求仕様

- `research_repo.toml` に登録された Repository と Adapter を対応付けられる。
- Repository 固有のデータ構造の解釈を Adapter に分離する。
- 複数 Repository で共通の Adapter を利用できる。
- Adapter を登録・検証・有効化でき、各状態を区別できる。
- Adapter のバージョン、実装形式、生成元を管理できる。
- 将来 AI が生成した Adapter を、既存の Adapter と同じ仕組みで受け入れられる。
- Artifact の識別情報、metadata、provenance、content hash、validation status、location を管理できる。
- Artifact の科学的整合性を検証でき、研究分野固有の検証にも対応できる。
- 要求条件に基づいて Artifact を検索し、実行環境からアクセス可能な場所を解決できる。
- 同一 Artifact が複数環境に存在する場合、それぞれの保存場所を管理できる。
- Planner / Executor が検証済み Artifact を利用できる。
- 未検証、不整合、アクセス不能、または曖昧な Artifact の自律的な再利用を防止する。

## 3. 設計上の制約

- Artifact Catalog 本体は Repository-agnostic とする。
- Repository 固有のパス、命名規則、schema を Catalog 本体に埋め込まない。
- Adapter の具体的な実装方式は担当者に委ねる。
- Adapter の登録と有効化を分離する。
- 後続の Phase 7B で、Catalog 本体の大幅な変更なしに Adapter 自動生成を追加できるようにする。
- 既存研究 Repository と既存 Artifact は原則 read-only とする。
- 既存 Registry、実行制御、承認機構との整合性を維持する。

## 4. 検証対象

最初の実データ検証には、以下の研究 Repository を使用する。

`https://github.com/inu-wanwan/active_space_research`

既存の Phase 4 orbital-level CASCI cache を利用し、N₂・BeO・NH₃、各3 geometry、k = 2〜10 の合計81条件について、研究条件と provenance の整合性を検証する。

この Repository は検証対象であり、Catalog 本体の前提条件ではない。

## 5. 完了条件

- 登録済み Repository から Artifact を発見・登録・検索・検証・解決できる。
- Repository ごとの Adapter が機能する。
- Artifact の provenance と整合性を追跡できる。
- 未検証または不整合な Artifact の自律的な再利用が防止される。
- 異なる Repository 構造でも Catalog 本体が機能する。
- Phase 4 CASCI cache の81条件を実データで自動発見・検証できる。
- Planner / Executor との連携が確認できる。
- Phase 7B の自動生成 Adapter を受け入れるための基盤が整っている。
