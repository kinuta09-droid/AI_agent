# Plan: NotebookLM + Gemini + Obsidian リサーチ高速化ワークフロー

> 出典: ユーザー提供動画 `VvE3jlZAVHc3cVzY.mp4` (約13分21秒, 英語音声, Whisper文字起こしベース)。
> 動画後半の有料チャンネル/コース宣伝パートは実装に無関係なため除外。
> 状態 (2026-08-16時点): `tools/tool_notebooklm.py` に実装済み。
> - Step2/8 (ソース投入→根拠付き質問): `run_notebooklm_research` (実装済み・ReAct登録済み)
> - Vault→NotebookLM差分同期: `sync_vault_export` (実装済み・日次バッチ登録済み、ReAct非公開)
> - Step2 (DeepSearch相当・自動ソース発見): `research_and_import` (実装済み・ReAct登録済み: `tool_notebooklm_research`)
> - Step6 (音声/動画/マインドマップ/インフォグラフィック/クイズ/フラッシュカード等の生成): `create_studio_artifact` +
>   `check_studio_status` (実装済み・ReAct登録済み: `tool_notebooklm_studio_create` / `tool_notebooklm_studio_status`)
> - Step7 (書籍の「読む価値」判定サブワークフロー): 未着手

## Goal

NotebookLM (Google) を情報源への「根拠付き検索エンジン」として使い、Gemini を「文章生成エンジン」として使い分けることで、
YouTube動画の重複視聴・既読済み書籍の再読・ハードウェア比較調査などにかかる時間を大幅に圧縮する。
最終的に得られた知見は Obsidian Vault に集約し、Vault自体を「まだ知らないこと」を判定する基準データとしても使う。

## 背景 (動画で語られた課題と効果)

| 課題 | 旧来のやり方 | ワークフロー導入後 | 動画内の実測値 |
|---|---|---|---|
| 新技術の学習 (同じ情報を繰り返す動画を大量視聴) | YouTube動画を何本も見て重複情報を手動で除去 | ソースをまとめて投入し要点だけ抽出 | 累計1,460時間削減(動画全体の主張) |
| 移住先の国の比較検討(8カ国) | 動画を延々視聴しObsidianへ手動転記 | チャンネルごとソース一括取込→比較プロンプト1回 | 3ヶ月(約360時間) → 15分 |
| 書籍が既知の内容かどうかの判定 | 年60冊読み、5〜10冊が既知の内容で無駄になる | 書籍PDF+Vaultエクスポートを照合し「読む価値なし」を判定 | 10冊分・約150〜200時間削減 |
| ハードウェア(PC/スマホ)選定 | モデルを1つずつ動画で調査 | 要件を投げて価格帯別に絞り込み | 1週間〜1ヶ月 → 5分 |

## Steps

1. **NotebookLMでトピック別ノートブックを作る**
   → 例: 「Relocation」のようにテーマ単位でノートブックを分離する。

2. **ソースを一括投入する**
   - YouTube動画/チャンネルは専用拡張機能で追加(チャンネル単位の一括取込に対応)
   - `Discover Sources` / DeepSearch的な自動検索でクエリから関連ソースを収集
   - PDF/テキストファイルの直接アップロード、Webサイトリンクの貼り付けにも対応
   → 結果: 1つのノートブックに複数ソース(動画・記事・PDF)が根拠として紐付く。

3. **分析プロンプトを設計する(自分の言葉→Geminiでリライト→NotebookLM用に整形)**
   - 出力はMarkdown形式、ポイントごとに1文
   - 各ポイントに必ず出典(ソース)を付ける
   - 比較したい観点をあらかじめ指定する(例: 医療・交通・ネット環境・気候・家賃相場)
   → 結果: NotebookLMは「捏造しない」設計(GPT系との違いとして動画内で強調)。ソースにない情報は書かない。

4. **分析結果をObsidianへ転記**
   → 各国/各トピックのノートとして保存し、後で比較・絞り込みに使う。

5. **同じチャットで追加質問(フォローアップQ&A)**
   → 例: 「この情報から総合ランキングを作って」。回答には引用元の動画箇所が添付される。

6. **副次的な出力形式を生成する**
   - 音声要約 / 動画要約 (移動中などのながら消化用)
   - マインドマップ、インフォグラフィック、比較表
   - Anki用フラッシュカード書き出し
   - 理解度確認用クイズ/テスト
   → 結果: 同じ根拠情報から用途別の成果物を再利用できる。

7. **書籍の「読む価値」判定フロー(サブワークフロー)**
   1. 書籍名でレビューをDeepSearch的に自動収集
   2. 書籍PDFをアップロード(非対応フォーマットはCalibre等で変換)
   3. 既存Obsidian Vaultを1つのPDF/テキストファイルにエクスポート
      - Pythonスクリプトでフォルダを再帰的に走査し、テキストを1ファイルに結合
      - 画像用フォルダなどはスキップ対象として除外する
   4. 「書籍PDF」+「Vaultエクスポート」+「レビュー」を同じノートブックに投入
   5. 「この本は自分にとって既知の内容か、読む価値があるか」を質問する
      → 既知の内容ならその根拠(Vault内の該当メモ)付きで「読まなくてよい」と回答させる
      → 価値があるなら該当章だけをピンポイントで指示させる(全部読まなくてよい)

8. **NotebookLMからGeminiへのハンドオフ**
   → NotebookLM単体は「正確な情報抽出」は得意だが、スクリプト生成・記事執筆・整形文書作成は苦手(動画内で「フォーマットが崩れる」と明言)。
   → Gemini側にNotebookLMのノートブックを直接連携させ、抽出済みの根拠情報をベースに実際の文章・スクリプトを生成させる。
   → 役割分担: **NotebookLM = 根拠付き検索・要約レイヤー / Gemini = 文章生成レイヤー**。

## Verification

- [ ] 実際のリサーチ課題(既存プロジェクトのVaultにある未整理トピックなど)で旧来手順との所要時間を比較する
- [ ] NotebookLMの出力の引用元が実在するソースと一致しているか(捏造がないか)を数件サンプルチェックする
- [ ] Obsidian Vaultエクスポート用スクリプトが画像/バイナリフォルダを正しく除外できているか確認する
- [ ] Anki書き出しファイルが実際にAnkiでインポートできるか確認する

## このプロジェクト(AI_agent)との接続案 (実装するかは要判断・現時点では未着手)

既存の `tool_paper_digest.py`(docling本文キャプチャ→要約→下書き生成)、`tool_vault_hybrid.py`(Vaultハイブリッド検索)、
`lib/paths.py` の `OBSIDIAN_VAULT_ROOT` と役割が重なる部分がある:

- **Vaultを1ファイルに書き出す処理**は、動画内で語られた「Vault→単一PDF/テキスト化スクリプト」と同種。既存の docling/検索基盤があるため、
  ゼロから作るより `tool_vault_hybrid.py` 側の検索結果を束ねる方が近道かもしれない。
- **「既に知っている内容かどうかを判定してから要約するかどうか決める」設計**は、`retrieval-tuning` skill や `llm-output-safety` skill が扱う
  「根拠のない出力を作らせない」という既存方針と親和性が高い。`tool_paper_digest.py` に「Vault既知判定ステップ」を足す拡張として検討可能。
- **NotebookLM自体をAPI経由で呼ぶ手段は既にローカルに存在する・認証済み**: `notebooklm-mcp-cli` (PyPI, MIT)。
  `notebook_create`/`source_add`/`research_start`(=DeepSearch相当の自動ソース発見)/`notebook_query`(分析プロンプト実行)/
  `studio_create`+`download_artifact`(音声・動画・マインドマップ・インフォグラフィック・クイズ・Anki等9種の出力生成)/
  `export_artifact`など約29ツールで、動画内の手順をほぼ1対1でAPI化できることを確認済み。
  → よって「Geminiで代替」ではなく、**NotebookLMを実際にツール呼び出しで操作する**設計が可能。
  → GLM等の他LLMでの代替は不可: grounded QA自体はRAGの一種でモデル非依存だが、NotebookLMの価値は
    YouTube/PDF取り込み・引用管理・Studio出力(音声/マインドマップ等)の周辺実装にある。他モデルでやるなら
    その取り込み〜生成パイプラインを自前で作る話になり、既に動いているnotebooklm-mcpを使う方が早い。

### notebooklm-mcp-cli セットアップ状況 (2026-08-15時点で解決済み)

- インストール先: `C:\Users\amami\.gemini\antigravity\notebooklm-mcp-cli\.venv\`(Antigravity由来)+ `uv tool install`版
  (`nlm`/`notebooklm-mcp` が `%USERPROFILE%\.local\bin` 経由でPATH上に露出)
- **バージョン**: 導入時点でv0.3.2/v0.4.9と古く、Googleが`notebooklm.google.com`→`notebook.google.com`へドメイン移行済みだった
  ため401エラーで機能せず。`uv tool upgrade notebooklm-mcp-cli` と
  `uv pip install --python <antigravity venv>/Scripts/python.exe --upgrade notebooklm-mcp-cli` で**v0.9.11**へ更新して解消。
- **既知バグ**: `refresh_auth` MCPツールはディスク上にトークンが「存在する」ことだけで成功判定し、期限切れかどうかを見ない
  (`notebooklm_tools/mcp/tools/auth.py`)。トークン切れ時は`nlm login`(Chromeでの再ログイン)が必要。
- **既知の環境要因**: Windows機ではChrome標準デバッグポート`9222`をLenovo Vantageのバッテリーウィジェット
  (`msedgewebview2.exe --remote-debugging-port=9222`)が常時占有しており、CDPベースの認証ツールがこのポートと衝突する
  ことがある。ただし`nlm login`内部の`find_available_port()`は自動で空きポートへ回避するため、実運用上は通常問題にならない。
- **Claude Code連携**: `~/.claude.json`の`c:/AI_agent`プロジェクトスコープに`mcpServers.notebooklm`として登録済み(個人ローカル設定、
  リポジトリの`.mcp.json`には非コミット)。
  **2026-08-16判明**: `c:/AI_agent`(小文字ドライブレター)は実際には未使用のキーで、このマシンで実際に使われているのは
  `C:/AI_agent`(大文字+スラッシュ、`lastSessionId`等の実績で確認)という別キーだった。`.claude.json`の`projects`は
  パス文字列をそのままキーにし正規化しないため、大文字小文字/区切り文字が違うだけで別プロジェクト扱いになり、
  登録したはずのMCPサーバーが実際に使われるキーには存在しない、という食い違いが原因だった。
  `C:/AI_agent`側にも同じ設定をコピー済み(バックアップ`~/.claude.json.bak-notebooklm-fix`)。
  MCPサーバー一覧はセッション起動時にのみ読み込まれるため、**次回セッション開始時**にToolSearchで疎通確認すること。
- 認証確認: `notebook_list`で実際のノートブック一覧(6件以上、既存の個人利用分)を取得できることを確認済み。

## Rollback

このドキュメントは実装計画のメモであり、コード変更を含まない。着手する場合は対象範囲(新規スクリプト or 既存tool拡張)を
別途Plan Modeで確定してから着手する。
