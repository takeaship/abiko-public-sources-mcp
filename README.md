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
- `just stop`でコンテナを停止します。Crawl4AIのみ停止します。MCPの一時データはMCPプロセスの終了時に片付きます。

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
| `fetch_document(url)` | HTML・PDFの取得と版の一時保持、前回成功版との変更比較 |
| `search_documents(query, limit)` | このプロセスで一時保持中の抽出本文を検索。空白区切りAND、Unicodeと空白を正規化 |
| `read_source(document_id, page, offset, length, representation)` | 一時保持中の版を参照。Crawl4AIの抽出本文とPDF原本の補助抽出を区別 |
| `render_pdf_page(document_id, page)` | 一時保持中の原本PDFの指定ページを画像表示。OCRは実行しない |

質問の解釈、URL・検索語の選択、回答の作成は、接続したAIクライアントが行います。対象は我孫子市公式サイトと同市の外部会議録サイトです。リンク発見はページ内リンクの取得であり、Web検索エンジンではありません。

## 保存と出典

通常の取得・リンク発見では、本文・ページ本文・原本を `$TMPDIR/abiko-public-sources-<uid>/process-*/`（未指定時はOSの一時ディレクトリ）へプロセスごとに保存します。最終参照から2時間で期限切れとし、合計500 MiBを超えると参照の古い版から原本ごと削除します。期限はアクセス時と30秒間隔で確認します。単独で上限を超える版は取得直後に期限切れになります。

正常終了・エラー終了・SIGINT/SIGTERMで専用ディレクトリを削除します。SIGKILLやホスト停止で残った領域は、次回起動時に所有ロックを確認して回収します。生存中の別プロセスの本文は読み書きしません。明示的な終了ツールはありません。

出典情報だけは利用者別の `ABIKO_HISTORY` にある `fetched_versions` テーブルへ残します。既定値は `$XDG_STATE_HOME/abiko-public-sources-mcp/history.sqlite3`（`XDG_STATE_HOME` 未指定時は `~/.local/state/abiko-public-sources-mcp/history.sqlite3`）です。版ID・URL・取得日時・題名・対象期間・原本ハッシュ・取得成否・エラー分類を保存し、本文・原本パス・ページ本文・リンク一覧・取得応答全体は含めません。Crawl4AIの取得応答全体（`native_request` / `native_response`）は一時領域にも保存しません。通常ログにも本文・原本・画像・取得応答を出しません。エラー詳細は応答本文を含まない分類に限定し、Crawl4AI API側の `backend_...` と対象資料側の `source_...` を区別します。

`read_source` と `render_pdf_page` は、本文が使えない既知IDに `status: expired`、出典情報 `source`、再取得先 `refetch_url` を返します。未知IDは `status: unknown` です。別プロセスの一時版や前回起動の版も本文を共有せず期限切れとして返します。版IDはURLと取得日時から作り、別内容へ上書きしません。

`fetch_document` の追加フィールド `content_change` は、前回成功した同一URLの原本ハッシュと比較します。`status` は `first`（初回）、`unchanged`（一致）、`changed`（不一致）、`fetch_failed`（native取得失敗）、`unavailable`（原本未取得）です。`previous_id` と `previous_sha256` で比較先を示し、取得失敗は内容変更に数えません。サイト全体の索引構築・索引検索は未実装です。[改善方針](docs/roadmap.md)の次のPRで、探索済みファイルを配布してローカルMCPから読取り専用で開く方式を追加します。配布索引は `ABIKO_INDEX`（既定 `index/abiko.sqlite3`）とし、作成日時・スキーマ版を持たせます。PR1はこのファイルを作成・読取り・更新しません。取得履歴とは別ファイルなので、索引の差替えでも履歴は残り、配布物へ履歴が混ざりません。両設定に同じファイルを指定すると起動を拒否します。

出典にはURL・取得日時・対象期間を示し、PDFはファイル内の物理ページ番号を使います。最新取得に失敗したとき、古い成功版へ黙って戻りません。未知の資料の年度・会議日は未確定として扱います。

`data/` は使われなくなった旧保存先です。通常の起動では読み書き・削除せず、移行もしません。不要なら利用者が手動で削除できます。`ABIKO_DATA` に空でないパスを明示した場合だけ、そのディレクトリに本文・原本を残す検証用モードになります。このモードは保存済み証跡の再利用を許し、期限・容量回収・終了時削除を行いません。空文字は未指定と同じ通常モードです。旧 `data/` を指定すれば明示的な再利用になります。

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

`just smoke`・`just bench`・`scripts/walkthrough.py` は `ABIKO_DATA=runs/<run>/data` と実行専用 `ABIKO_HISTORY=runs/<run>/history.sqlite3` を指定し、本文・原本・MCP呼出しログを検証証跡として残します。通常ログとは区別してください。実サイト検証は対象サイトへアクセスします。公開リポジトリには取得済み本文・原本・実行ログを含めません。評価定義は[evaluation/](evaluation/)にあり、取得成功や検索到達を回答正解率として扱いません。

## 制約

外部会議録の本文収集、サイト全体の索引、巡回による更新・削除検知、OCRは未実装です。画像PDFと複雑な表は、画像を読めるクライアントで原本を確認します。予定・提案・決定・実施、議会と審議会を区別し、未取得の資料を「議論や要望がない」根拠にしません。

実行時は対象ページ・ページ内アセットへ通信します。初期導入ではソース・コンテナイメージ・Python依存を配布元から取得します。
