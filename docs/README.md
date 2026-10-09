# docs/

Research Autopilot の開発ドキュメント置き場です。共通開発ルールはリポジトリ直下の `AGENTS.md` を参照してください。

## 構成

```text
docs/
├── README.md                # このファイル（docs の運用ルール）
├── development_handoff.md   # Phase 共通の引き継ぎ記録
├── phase1_technical_design.md # Phase 1 公式資料調査と実装設計
├── operations/              # 運用手順（Phase 1 で作成）
│   ├── deployment.md        #   構築・デプロイ・更新
│   ├── monitoring.md        #   監視・Tailscale・通知
│   └── recovery.md          #   障害復旧・バックアップ / リストア
└── phase_specs/             # Phase 別の仕様書（各 Phase の実装依頼時に作成）
    ├── phase1.md
    ├── phase2.md
    ├── phase3.md
    ├── phase4.md
    └── phase5.md
```

`phase_specs/` 以下は、各 Phase の実装を依頼する時点でユーザーが提供する要件定義を格納します。現時点では `phase1.md` のみ存在します。

## 各ファイルの役割

| ファイル | 役割 | 誰が変更するか |
|---|---|---|
| `phase_specs/phaseN.md` | Phase N の仕様の正本（ローカル SSOT） | ユーザー、またはユーザーの承認を得たエージェント |
| `phase1_technical_design.md` | Phase 1 の公式資料調査、設計判断、実装契約 | ユーザーが調査・設計を依頼したエージェント |
| `operations/*.md` | 運用手順。検証済みの範囲を明記する | 実装・運用を担当するエージェント |
| `development_handoff.md` | 進捗・状態・次の作業の記録。仕様の正本ではない | 作業中のエージェント |

## 運用ルール

1. **仕様書は勝手に変更しない。** 仕様書の作成・変更はユーザーの指示または承認があった場合のみ行う。
2. **仕様書がない Phase の実装を始めない。** 要件を推測で補わず、ユーザーに確認する。
3. **判明した事実・設計判断は仕様書ではなく記録に残す。** 開発中に分かった制約、仕様の不明点・矛盾、採用した設計判断とその理由は `development_handoff.md` に記録し、仕様への反映が必要ならユーザーに提案する。
4. **引き継ぎ記録はこまめに更新する。** 重要な実装単位の完了時、セッション終了・中断前、ブロッカー発生時に `development_handoff.md` を更新する。
5. 新しい種類のドキュメント（設計判断記録 ADR、運用手順書など）が必要になった場合は、追加する前にユーザーに確認し、追加したらこの README の構成に追記する。
