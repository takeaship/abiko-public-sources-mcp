# 我孫子市公開資料MCP（Crawl4AI）

我孫子市の公開資料を取得・検索・原文参照する、ローカル実行のMCPサーバーです。MCP対応のAIクライアントから、市民の関心に沿って資料を調べるために使います。

現在は資料取得の試作段階です。市民向けの独立したチャット画面や回答生成モデルは含みません。[現状の機能](docs/capabilities.md)、[市民向け調査の仕様](docs/citizen-research.md)、[14問の試用結果](docs/trial-results.md)、[改善方針](docs/roadmap.md)を参照してください。

## 起動と接続

必要なものはDocker EngineとCompose、Git、openssl、uv、justです。Pythonはuvが用意します。

```sh
git clone https://github.com/takeaship/abiko-public-sources-mcp.git
cd abiko-public-sources-mcp
just setup
just up
just smoke
```

`setup`はCrawl4AIの参照用ソースrevisionを確認し、未作成時だけ`.env`にローカル用tokenを生成します。Crawl4AI 0.9.5の公式イメージはdigestで固定しています。初回のDockerイメージ取得には数GBの空き容量が必要です。

- APIは`http://127.0.0.1:11335`。token認証を使い、loopbackにのみ公開します。
- `just mcp`でstdio MCPサーバーを起動します。
- `just stop`でコンテナを停止します。取得済みのローカルデータは残ります。

クライアントのMCP設定例です。`/absolute/path/to/abiko-public-sources-mcp`をclone先の絶対パスに置き換えてください。

```json
{
  "mcpServers": {
    "abiko-public-sources": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/abiko-public-sources-mcp", "run", "python", "server.py"]
    }
  }
}
```

## 現在の5ツール

| ツール | 動作 |
|---|---|
| `discover_links(url, contains, limit)` | 1ページを取得してリンクを発見。次にたどるURLはクライアントが選ぶ |
| `fetch_document(url)` | HTML・PDFの取得と版の保存 |
| `search_documents(query, limit)` | 保存済みの抽出本文を検索。空白区切りAND、Unicodeと空白を正規化 |
| `read_source(document_id, page, offset, length, representation)` | 保存版の本文を参照。Crawl4AIの抽出本文とPDF原本の補助抽出を区別 |
| `render_pdf_page(document_id, page)` | 原本PDFの指定ページを画像表示。OCRは実行しない |

質問の解釈、URL・検索語の選択、回答の作成は、接続したAIクライアントが行います。対象は我孫子市公式サイトと同市の外部会議録サイトです。リンク発見はページ内リンクの取得であり、Web検索エンジンではありません。

## 現在の保存動作と、次の変更

現在の実装は、本文・原本・取得応答を既定の`data/`へ保存します。`ABIKO_DATA`で保存先を変更できます。リンク探索時にも取得版を保存します。これらの保存先や検証ログはGit管理の対象外です。

次の改善では、サイトマップや一覧ページを起点に広い範囲を探索し、URL・資料名・分類・日付・取得状態などの索引を常設する方針です。本文・PDFは質問に必要なときに取得して一時保持し、処理終了後に削除する構成を検討しています。この保存方式とサイト全体の索引構築は、まだ実装していません。

出典にはURL・取得日時・対象期間を示し、PDFはファイル内の物理ページ番号を使います。最新取得に失敗したとき、古い成功版へ黙って戻りません。未知の資料の年度・会議日は未確定として扱います。

## 検証

`just setup`後に実行します。

```sh
just check          # 合成データを使う回帰テストとlint
just smoke          # 実サイトの取得・検索・出典参照
just bench          # 選定20資料・16質問の技術検証
just details        # PDF明細・HTML表の追加2質問
uv run python scripts/summarize.py runs/<run-directory>
uv run python scripts/walkthrough.py runs/<run-directory>
```

実サイト検証は対象サイトへアクセスし、`runs/`に本文・原本・MCP呼出しログを作成します。公開リポジトリには取得済み本文・原本・実行ログを含めません。評価定義は[evaluation/](evaluation/)にあり、取得成功や検索到達を回答正解率として扱いません。

## 制約

外部会議録の本文収集、サイト全体の索引、更新・削除検知、OCRは未実装です。画像PDFと複雑な表は、画像を読めるクライアントで原本を確認します。予定・提案・決定・実施、議会と審議会を区別し、未取得の資料を「議論や要望がない」根拠にしません。

実行時は対象ページ・ページ内アセットへ通信します。初期導入ではソース・コンテナイメージ・Python依存を配布元から取得します。
