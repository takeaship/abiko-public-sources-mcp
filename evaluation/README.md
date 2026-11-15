# 評価データの役割

| ファイル | 目的 | 現状 |
|---|---|---|
| `corpus.json`・`questions.json`・`followup-questions.json` | 選定資料の取得・抽出・検索・原文参照の技術回帰試験 | `just bench` / `just details` で実行。API成功・語句包含・top5到達を回答正解率に置き換えない |
| `citizen-scenarios.json` | 市民の抽象的な関心から複数資料を調べる、今後の主たる受入評価 | 4シナリオとも未評価。参照資料一式と実クライアントでの調査・回答の人による確認が必要 |

目標と判断のルールは [市民向け調査の仕様](../docs/citizen-research.md)。`citizen-scenarios.json` を既存の単一資料・語句一致用benchmarkへそのまま渡さない。資料取得と、市民が知りたいことに答えられることは、別の層の検証として記録する。
