# Phase 7B — Autonomous Adapter Generation

（2026-10-11 ユーザー提供。親仕様は `phase7.md`。ユーザー指示により、この時点では Phase 7A の実装までで停止し、7B は未着手。）

## 1. 目的

`research_repo.toml` に登録された Repository に対応する Adapter が存在しない場合、ResearchAutopilot が AI を利用して Adapter を自動生成・検証し、所定の条件に従って有効化できるようにする。

これにより、新しい研究 Repository の導入時に必要な人間の実装作業を削減する。

## 2. 要求仕様

- Adapter が未登録、未対応、または更新を必要とする Repository を検出できる。
- AI が Repository の構造、データ形式、metadata、manifest などを調査し、対応する Adapter を生成できる。
- 生成された Adapter が Phase 7A の共通インターフェースに適合する。
- Adapter の生成元、対象 Repository、バージョン、検証結果を追跡できる。
- 生成された Adapter の動作と安全性を自動検証できる。
- 検証に失敗した Adapter は有効化されない。
- 既存の有効な Adapter を、未検証の生成物で上書きしない。
- Adapter が有効化された後、Phase 7A の Catalog から利用できる。
- Adapter の更新・再生成においても、既存の検証・承認条件を維持する。

## 3. 有効化・承認方針

### Declarative Adapter

- AI による自動生成を許可する。
- 定められた自動検証に合格した場合、人間の承認なしで有効化できる。
- 宣言的な定義から、任意のプログラム実行や未認可のファイルアクセスが可能にならないことを要件とする。

### Python Adapter

- AI による自動生成を許可する。
- 隔離された環境で安全性・動作を検証する。
- 自動検証に合格しても、人間の明示的承認なしに有効化しない。
- 未承認の Adapter を ResearchAutopilot 本体で実行しない。

### Artifact の再利用

Adapter の有効化と、Artifact の科学的妥当性は独立した判断とする。

有効化された Adapter が発見した Artifact であっても、必要な科学的整合性が検証できなければ自律的に再利用しない。

## 4. 設計上の制約

- Phase 7A の Catalog と Adapter 管理機構を再利用する。
- Adapter 生成機能の追加によって、Catalog 本体に Repository 固有のロジックを導入しない。
- 生成対象は、可能な限り Declarative Adapter を優先する。
- Python Adapter が必要な場合でも、実装方法は担当者に委ねる。
- 生成・検証・有効化の過程を追跡可能にする。
- 既存の承認機構、権限管理、Repository Registry と整合させる。

## 5. 完了条件

- 未対応 Repository に対して AI が Adapter を生成できる。
- 生成した Adapter が共通の検証に合格した場合、Artifact Catalog で利用できる。
- Declarative Adapter は検証合格後に自動有効化できる。
- Python Adapter は人間の明示的承認後にのみ有効化できる。
- 検証失敗・未承認の Adapter が利用されない。
- Adapter の生成・更新履歴と検証結果を追跡できる。
- 複数の異なる Repository 構造で自動生成と導入を検証できる。
- 既存 Phase 7A の動作を維持できる。
