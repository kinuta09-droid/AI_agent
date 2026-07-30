# Docling 導入計画 — エージェントの標準ドキュメント取り込み経路にする

ブランチ: `claude/docling-paper-capture-system-fbb3ug`

## 役割分担 — どのセッションが何をやるか

| セッション | 環境 | やること |
|---|---|---|
| **リモートセッション** (Linux サンドボックス) | ネットワーク制限あり・GPU無し・Vault無し | この計画書のコミットと、Phase 1〜3 の**コード実装**。ただし docling 本体は入れられない (実測は不可) |
| **実機セッション** (Windows / `C:\AI_agent`) | 本番 Vault・GPU・Ollama | **Phase 0 の実測と Go/No-Go 判定**、および各 Phase の検証 |

### 実機セッションの始め方
```
cd C:\AI_agent
git fetch origin claude/docling-paper-capture-system-fbb3ug
git checkout claude/docling-paper-capture-system-fbb3ug
```
Claude Code を起動して:
> `plans/docling-integration.md` を読んで、進捗表の「未着手」の先頭から始めて

---

## 進捗表 — **中断・再開はここを見る**

各 Phase を終えたら**この表を更新してコミットする**。次のセッションは
この表だけ読めば、どこから再開すればよいか分かる (会話履歴に依存しない)。

| # | 内容 | 状態 | 実施日 | 備考 / 実測値 |
|---|---|---|---|---|
| 0-1 | torch (CPU版) + docling インストール | 未着手 | | |
| 0-2 | モデル事前DL + `DOCLING_ARTIFACTS_PATH` 設定 | 未着手 | | |
| 0-3 | 実物3検体でベンチ (`scripts/bench_docling.py`) | 未着手 | | 秒数/文字数/表数を下の表に記入 |
| 0-4 | **Go / No-Go 判定** | 未着手 | | |
| 1 | 変換層の一般化 (ローカル対応・mtimeキャッシュ・表の保持) | 未着手 | | |
| 2 | `tool_document.py` + registry + Discord `/doc` | 未着手 | | |
| 3-0 | サイドカー変換 (方式A) + スケジューラ 03:05 ジョブ | 未着手 | | インデクサ本体は触らない |
| 3-1 | **チャンカーの表分割修正** (docling 非依存・単独で効く) | 未着手 | | Phase 0 が No-Go でも実施可 |
| 3-2 | 表キャプションを添えて埋め込み | 未着手 | | 3-4 の実測で不要なら**やらない** |
| 3-4 | `eval_retrieval` に表/PDFクエリを追加して before/after | 未着手 | | **3-1 の前に物差しを作る** (3-3 は判断のみで作業なし) |
| 4 | 表の計算経路 (CSV/SQLite) | 保留 | | 数値質問の実需が出たら |

状態は `未着手` / `進行中` / `完了` / `見送り(理由)` のいずれかで書く。

### Phase 0-3 ベンチ結果 (実機が記入する)

| 検体 | 変換秒数 | 出力文字数 | 表の数 | 日本語の可否 | 表がMarkdown tableか |
|---|---|---|---|---|---|
| arXiv 英語 PDF | | | | | |
| 日本語 PDF | | | | | |
| .docx / .pptx | | | | | |

`torch.cuda.is_available()` = (記入)  /  docling バージョン = (記入)

---

## Context — 今どこまで出来ているか

| 要素 | 状態 |
|---|---|
| `lib/docling_client.py` (変換層・段階フォールバック) | 実装済・**未実行**。docling が無いので常に HTML 経路 |
| `requirements-docling.txt` | 作成済 (`docling>=2.0.0` のみ)。**torch の入れ方は未定義** ← Phase 0 の主題 |
| `tools/tool_paper_radar.py` / `tool_paper_digest.py` | 実装済。docling が入れば本文品質が上がる (入らなくても動く) |
| Vault 意味検索 (`tool_vault_index`) | `.md` のみ走査。**PDF/Office は検索対象外** ← Phase 3 の主題 |
| Discord の添付ファイル処理 | 無し ← Phase 2 の主題 |

つまり「Docling を導入する」とは、**受け皿に本体を入れて、エージェントの3つの入口
(ツール / Discord / 意味検索) に配線すること**を指す。

---

## Goal

PDF・Office・画像を **Markdown に構造化して取り込む経路をエージェントの標準**にする。
到達点は次の3つが実機で動くこと:

1. `/doc` に PDF を添付 → 要約が返り、Vault にノートが残る
2. Vault に置いた PDF が `/agent` の意味検索でヒットする
3. docling を**アンインストールしても**全機能が HTML/要旨フォールバックで動き続ける
   (劣化したことは backend 表示で必ず分かる)

---

## Phase 0: 実機で導入コストを測る ← **判断ゲート。ここが No-Go なら以降はやらない**

### 0-1. torch を CPU 版で先に入れる (推奨・順序が重要)

```bat
cd C:\AI_agent
antigravity_env\Scripts\activate
py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
py -m pip install -r requirements-docling.txt
```

**なぜ CPU 版か** (このマシン固有の判断):
- GPU は Ollama (`bge-m3` 埋め込み) と ComfyUI/LoRA が既に使っている。
  `tool_activity_index` の docstring にある通り、VRAM 競合は「実行条件が
  直前の操作で変わる」再現性の悪いバグを生む。docling は日に数本しか回さないので
  CPU で足りる。
- CUDA 版 torch は 2.5GB 超。requirements-docling.txt を素で入れると CUDA 版が
  引かれる可能性があるため、**torch を先に CPU 版で固定してから** docling を入れる。

確認:
```bat
py -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```
→ `cuda.is_available()` が **False** であること (True なら Ollama と競合しうる)。

> Python 3.13 前提 (`pyrightconfig.json` / CI と同じ)。torch/docling が 3.13 の
> ホイールを持っているかはこの手順で判明する。入らなければ **No-Go ではなく**
> 「3.12 の別 venv を docling 専用に切って subprocess 越しに使う」案へ分岐
> (Phase 0-4 参照)。

### 0-2. モデルを事前ダウンロードする

```bat
docling-tools models download
```
初回変換時に ~1GB のモデル取得が走ると、Discord コマンドが数分無応答になる
(ユーザーからは「壊れた」と区別がつかない)。**先に落として置き場を固定する**:

`.env` に1行:
```
DOCLING_ARTIFACTS_PATH=C:\AI_agent\models\docling
```
(Phase 1 でこの環境変数を `lib/docling_client.py` が読むようにする。
 リポジトリ外の絶対パスなので、**参照するのは `lib/paths.py` 経由**にすること。
 `verify_project.check_hardcoded_abs_paths()` に引っかかる書き方をしない)

### 0-3. 実物 3 本でベンチを取る

新規 `scripts/bench_docling.py` (リモートセッションで実装可) が、
指定した実ファイルを変換して JSON + Markdown で `logs/docling_bench_*.json` に残す:

| 検体 | 見るところ |
|---|---|
| arXiv の英語論文 PDF | 所要時間・表が Markdown table になるか・数式の扱い |
| **日本語の PDF** (請求書・行政資料など手元のもの) | **文字化けしないか** (cp932 環境で最も壊れやすい) |
| .docx か .pptx | Office 系が同じ API で通るか |

記録する値: 変換秒数 / 出力文字数 / 表の数 / 警告 / ピークメモリ。

### 0-4. Go / No-Go の判定基準

| 条件 | Go | No-Go の場合 |
|---|---|---|
| 1本あたり **120秒以内** (CPU) | ✅ | 週次バッチ専用に格下げ (Discord 同期実行はしない) |
| 日本語が化けない | ✅ | OCR 有効化を試す → それでも駄目なら PDF は HTML/pdfminer 経路に留める |
| 表が Markdown table になる | ✅ | 構造化の主目的が消えるので **導入見送り** (HTML 経路のままで十分) |
| torch が Python 3.13 に入る | ✅ | docling 専用 venv + subprocess 経由に設計変更 (Phase 1 の実装が変わる) |

**結果は冒頭の「Phase 0-3 ベンチ結果」表に記入してコミットしてから Phase 1 に進む。**
実測値を残さないと、後から「なぜ CPU 版なのか」「なぜ週次なのか」が失われる。

> 表が Markdown table にならなかった場合でも、**Phase 3-1 (チャンカーの表分割修正)
> だけは実施する**。あれは docling とは独立に、既に Vault にある Markdown の表が
> 壊れている問題を直すものなので、No-Go でも価値が残る。

---

## Phase 1: 変換層の一般化 (`lib/docling_client.py`)

現状は「論文PDFのURL」を前提にしている。汎用ドキュメント取り込みに広げる。

1. **ローカルファイル対応 + キャッシュキーの修正 (最優先)**
   今のキャッシュキーは URL 文字列のみ。ローカルファイルだと
   **同じパスのファイルを更新しても古い変換結果が返る** = 無言で嘘をつく
   (CLAUDE.md「表示と実態の食い違い」の同型)。
   → キーに `(パス, mtime, size)` を含める。
2. **フォーマット別オプション**: `do_ocr` / `do_table_structure` を引数で受ける。
   **OCR は既定 off** (数倍遅い)。`ocr=True` を明示したときだけ有効。
3. **`artifacts_path`** を `DOCLING_ARTIFACTS_PATH` (`lib/paths.py` 経由) から読む。
   未設定なら docling 既定 = 現行挙動のまま (既存環境に .env 追記を強制しない)。
4. **スレッド数の制御**: docling の設定オブジェクト経由で行う。
   `os.environ["OMP_NUM_THREADS"] = ...` は **LEVEL 2 で禁止**
   (子プロセス全体を汚染する)。どうしても必要なら subprocess の `env=` (LEVEL 1)。
5. `is_available()` の結果をプロセス内でキャッシュ (今は呼ぶ度に import を試みる)。
6. **`DoclingDocument` を捨てない (表を活かすための前提)**
   現行実装は `export_to_markdown()` **だけ**を呼んでおり、変換結果の
   ドキュメントオブジェクトを即座に平文化して捨てている。
   その瞬間に **表の構造とキャプションが失われる** ので、表を扱う道が塞がる。
   → 変換結果から `tables` を別途取り出し、戻り値に
   `{"text": ..., "tables": [{"markdown":..., "caption":..., "csv":...}, ...]}`
   の形で同梱する。`text` 側の互換は保つ (既存の呼び出し元を壊さない)。
   docling 側の正確な API 名 (`doc.tables` / `export_to_dataframe()` /
   `caption_text()` 等) は **Phase 0 で実物を見て確定する**。
   ここが取れないなら Phase 3 の L2/L3 は着手しない (材料が無い)。

**テスト** `tests/test_docling_client.py` (新規・docling 不要):
- mtime が変わったら再変換される / 変わらなければキャッシュが効く
- `ocr=True/False` がパイプラインオプションに伝わる (スタブで受け取り確認)
- docling 不在 → HTML → 全滅で `CaptureError`、の順序 (既存 `test_paper_radar.py` [9] から移設)
- 表を含む変換結果で `tables` が埋まり、`text` 側の互換が保たれること

---

## Phase 2: エージェントのツールにする

### 2-1. `tools/tool_document.py` (新規)
| 関数 | 役割 |
|---|---|
| `tool_read_document(path_or_url, ocr=False)` | Markdown を返すだけ (ReAct から呼ぶ材料取得) |
| `tool_import_document(path_or_url, folder='30_Main')` | 変換 → Gemini 要約 → Vault にノート保存 (元ファイルへのリンク付き) |

戻り値契約は既存どおり `"Error: ..."`。`lib.bot_errors.is_failure_result()` が
拾うことをテストで固定する。

### 2-2. レジストリ
`lib/tool_registry.py` に 2 エントリ (lazy wrapper)。
**どちらも `parallel_safe` は付けない** — 変換キャッシュと Vault に書くため。
`tests/test_tool_registry.py` の `MUST_NOT_BE_PARALLEL_SAFE` に追記
(これを忘れると `test_parallel_safe_covers_every_entry` が落ちて気付ける)。

### 2-3. Discord `/doc` + 添付ファイル
- `discord.Attachment` を受け取り、一時ディレクトリへ保存 → 変換 → 要約 → 返信。
- **`await interaction.response.defer()` を最初に**。docling は数十秒かかるので
  3秒の interaction token で必ず落ちる (skill `discord-bot-patterns`)。
- 添付サイズ上限 (25MB) とページ数上限を先に弾く。無制限に受けると
  数百ページの PDF で CPU を数十分占有する。
- 一時ファイルは `finally` で削除。

---

## Phase 3: 意味検索への接続 ← **投資対効果が最大**

現状 `tool_vault_index._walk_vault_md()` は `.md` しか歩かないので、
Vault に PDF を置いても `/agent` の検索には**永久に出てこない**。

### 方式A (推奨): サイドカー変換方式
Vault の PDF/DOCX/PPTX を検出 → docling で `.md` に変換して
`99_Assets/Converted/<元のパス>.md` に置く → **既存インデクサがそのまま拾う**。

- 利点: インデクサ本体 (`tool_vault_index`) を1行も触らない = 回帰リスクが最小。
  変換に失敗しても index は従来通り動く。
- 変換済みノートの先頭に元ファイルへのリンクと `source: docling` を書く
  (検索で当たったとき、それが変換物だと分かるようにする)。
- 新規ジョブ `Document Conversion` をスケジューラの **03:05** に置く
  (03:15 の Vault Semantic Index の前。順序が逆だと1日遅れて反映される)。

### 方式B (不採用): インデクサに変換を組み込む
再構築のたびに全 PDF を再変換しかねず、失敗が index 全体を巻き込む。

### 3-1. 表がチャンカーで壊れるのを直す ← **docling と独立。単独でも効く**

**現行コードの実害** (`tools/tool_vault_index.py`):
- `CHUNK_MAX_CHARS = 1200` / 分割は `_split_by_paragraphs()` が **`\n\n` (空行) 境界**
- ところが **Markdown の表は空行を含まない1ブロック**。1200字を超える表は
  単一段落として扱われ、次のハードスプリットに落ちる:
  ```python
  while len(para) > max_chars:
      head, para = para[:max_chars], para[max_chars - overlap:]  # 文字位置で機械的に切断
  ```
- 結果、**行の途中で切れ、2片目以降はヘッダ行を失う**。検索でヒットしても
  `| 3 | 0.82 | 0.71 |` のように列名の無い数字の羅列になり、LLM は解釈できない。

修正 (純関数なのでテストが容易):
1. 表ブロック (`|` 始まりの連続行) を検出し、**行単位で分割**する
2. 分割したら各片の先頭に **ヘッダ行と `|---|` 区切り行を再掲**する
3. 表が丸ごと `max_chars` 未満なら**絶対に割らない**

テスト `tests/test_vault_chunking.py` (新規):
- 1200字超の表を食わせて、全チャンクがヘッダ行を持つこと
- 表の行が途中で切れていないこと (各行が `|` で始まり `|` で終わる)
- 表を含まない既存ノートのチャンク結果が**1文字も変わらない**こと (回帰の逆方向)

> **この項目は docling を入れなくても価値がある。** 既に Vault にある
> Markdown の表が同じ壊れ方をしているので、Phase 0 が No-Go でも実施してよい。

### 3-2. 表にキャプションを添えて埋め込む (docling がある場合)

数値の格子はベクトルとして情報が薄く、意味検索でまず釣れない。
**表の直前に自然文の説明を1行置いてから埋め込む**。

- **第一候補は元文書のキャプション** (`Table 1: ...`)。docling が保持しているので
  **LLM 呼び出しゼロ** (Phase 1-6 で取り出せるようにしておく)
- キャプションが無い表**だけ** Gemini で1文生成する。変換キャッシュに乗るので
  **同じ表につき生涯1回**。`DAILY_GEMINI_CALL_LIMIT` (既定200) を圧迫しない
- 生成文は「この表は〜の比較で、最良は〜」の形。数値は**表にある値のみ**

### 3-3. チャンク分割の乗り換え判断
docling には構造を保つ `HybridChunker` があるが、既存の `_chunk_markdown` と二重になる。
**まずは 3-1 を入れた既存チャンカーで通す**。乗り換えは `scripts/eval_retrieval.py` の
before/after を測ってから (skill `retrieval-tuning` の作法)。

### 3-4. 検証
`scripts/eval_retrieval.py` に次を**追加してから**着手する (先に物差しを作る):
- **PDF 由来ノートを狙うクエリ** — 変換経路が効いているか
- **表の中身を問うクエリ** (例:「手法Aの精度は」「表の最良値は」) — 3-1/3-2 の効果測定。
  **3-1 だけ入れた状態で何%当たるかを先に測る**。そこで足りて
  いれば 3-2 (Gemini を使う側) は不要
- 既存クエリのスコアが**劣化しないこと** (変換ノートがノイズとして混ざる副作用)

---

## Phase 4 (任意): 既存経路の見直し / 表の計算経路

- **表を CSV/SQLite に落として「計算はコードでやる」経路** (表の L3)。
  着手条件は **「表の数値計算を求める質問が実際に来たとき」**。
  大きな表を LLM に読ませて算術させるのは精度でもコストでも負け筋なので、
  `TableItem` → DataFrame → CSV を `99_Assets/Converted/tables/` に出し、
  数値質問は検索ではなく計算に回す。**先回りで作らない** (使われない可能性が高い)
- `tool_fetch_url` の HTML 抽出を docling に寄せる → **やらない方に倒す**。
  bs4 はミリ秒、docling は秒。動的サイトは `tool_browser_use` が既に担当。
  docling を使うのは「表・レイアウトの構造が要る」ときだけ、と線を引く。
- 音声 (`tool_audio_automation`) との統合は別件。docling の ASR 経路は
  既存の Whisper 系フローと二重になるので今回は触らない。

---

## 判断ログ: 表のためにグラフ構造化 (GraphRAG) は採らない

**Decision**: 表の扱いは「チャンカー修正 (3-1) → キャプション付与 (3-2) →
必要なら計算経路 (Phase 4)」の3層で解く。エンティティグラフは**構築しない**。

**Reason**:
1. **問題の所在が違う**。表が使えない原因は
   「1200字超の表が文字位置で切断されヘッダを失う」という**チャンカーの欠陥**で、
   グラフを足しても材料が壊れたままなので直らない
2. **グラフが解くのは文書をまたぐ多段推論**
   (「この論文の著者が過去に提案した手法を引用した別論文の結果は」)。
   表の参照は**1文書内**の話で、道具の適用先が違う
3. **コストが桁違い**。グラフ構築はチャンク1件ごとに LLM でエンティティ抽出が要り、
   `DAILY_GEMINI_CALL_LIMIT` (既定200) を初回構築で突破する。
   しかもノートを編集するたびに再構築が要る。
   一方キャプション方式は「キャプションを持たない表だけ・生涯1回」で済む
4. **Obsidian Vault は既にグラフである**。`[[wikilink]]` と MOC
   (`tool_obsidian._determine_parent_moc` が自動付与) がエッジそのもの。
   多段推論が本当に必要になったら、**まず既存のリンクを辿る実装**を試す。
   別のグラフを建てるのは、それが足りないと実測できてから

**Alternatives considered**:
- *GraphRAG (エンティティ+関係抽出)*: 上記コストで却下。将来
  「文書をまたぐ質問が頻発する」と実測できたら再検討
- *docling `HybridChunker` への全面移行*: 構造保持は魅力だが既存チャンカーと
  二重になる。3-1 で足りるかを測ってから (3-3)
- *表を最初から全部 SQLite に*: 使われるか分からない機能に変換コストを払う。
  実需が出てから (Phase 4)

---

## Verification — 各フェーズで「動いた」と言える根拠

- [ ] **Phase 0**: `py scripts/bench_docling.py` の出力 (3検体の秒数・文字数・表数) を
      本計画書に貼る。`torch.cuda.is_available() == False` を確認
- [ ] **Phase 1**: `py tests/test_docling_client.py` 緑。同じパスのファイルを
      更新 → 再変換されること (mtime キャッシュの実挙動)
- [ ] **Phase 2**: `py verify_project.py` (フル) 緑。Discord で実際に PDF を添付 → 要約が返る。
      `py tests/test_document_tool.py` 緑
- [ ] **Phase 3-1**: `py tests/test_vault_chunking.py` 緑。1200字超の表が
      行の途中で切れず、全チャンクがヘッダ行を持つこと。表を含まないノートの
      チャンク結果が**変わらない**こと
- [ ] **Phase 3**: `py scripts/eval_retrieval.py` の before/after を並べる。
      PDF 由来クエリ・**表クエリ**が当たり、既存クエリが劣化していないこと。
      3-1 だけで表クエリが通るなら 3-2 は**やらない**
- [ ] **全体**: `tests/test_*.py` スイープ (現在70本) が緑
- [ ] **最重要**: `py -m pip uninstall docling` した状態で上記スイープが**そのまま緑**。
      docling は「あれば品質が上がる」オプションであって、
      **無いと壊れる依存にしない**。これが崩れたら設計が失敗している

---

## Rollback

| Phase | 戻し方 |
|---|---|
| 0 / 1 | `py -m pip uninstall docling torch` のみ。コードは docling 不在で HTML 経路に落ちるので**削除不要** |
| 2 | registry の2エントリと `bot_discord.py` のコマンド定義を revert (ツールファイルは残っても無害) |
| 3 | `99_Assets/Converted/` を削除 → 次回 index で消える。スケジューラの `Document Conversion` ジョブを外す |

**各フェーズを1コミットにして revert 単位を揃える** (CLAUDE.md: 動いた時点で即コミット)。

---

## リスク一覧

| リスク | 症状 | 対処 |
|---|---|---|
| torch が CUDA 版で入る | Ollama 埋め込み/ComfyUI と VRAM 競合。「夜だけ遅い」等の再現しにくい劣化 | Phase 0-1 の順序 (CPU版 torch を先に固定) + `cuda.is_available()` 確認 |
| 初回変換でモデル DL が走る | Discord が数分無応答 = 壊れたように見える | `docling-tools models download` を先に実行し `DOCLING_ARTIFACTS_PATH` を固定 |
| 日本語 PDF の文字化け | 要約が静かに壊れる (エラーは出ない) | Phase 0-3 で実物を検体にする。目視確認を判定基準に入れてある |
| 巨大 PDF で CPU 占有 | 他ジョブが詰まる | ページ数・サイズの上限を Phase 2 で先に弾く。重い処理は夜間バッチへ |
| CI が重くなる | tests が数分伸びる | `requirements.txt` には**入れない** (現状維持)。CI は常に「docling 不在の経路」を検証する側に回る = フォールバックの常時テストになる |
| docling の API が版で変わる | import/属性エラー | `lib/docling_client.py` に閉じ込め済み。バージョン確認に `context7` MCP を使いたいが**現在未認証** (使うなら `/mcp` で認証が要る) |

---

## 決めてほしいこと (Phase 0 の前に1つだけ)

**torch を CPU 版で固定してよいか。** 推奨は CPU 版 (上記の理由)。
GPU を使いたい場合は、Ollama の `OLLAMA_KEEP_ALIVE` を短くする等の
VRAM 調停をセットで決める必要があり、Phase 0 の手順が変わる。
