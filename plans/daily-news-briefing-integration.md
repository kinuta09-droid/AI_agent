# Daily News × Morning Briefing 統合 計画書

作成: 2026-07-30 / 更新: 2026-07-30 (スコープ拡大: ニュース要約の二重実装の解消を含む)
ブランチ: `claude/daily-news-briefing-integration-qtr5qo`
方式: **保存済み News の成果物を読む Provider** (ニュース取得は Briefing の外で走らせる)

---

## 背景 — 今どうなっているか

現状、朝の情報は2本の独立したジョブに分かれている (`agent_scheduler.py:274-312`)。

```
07:00  Daily Note Generation   generate_daily_note(include_news=False)
07:30  Morning Briefing        _run_morning_briefing()  → Discord 投稿 (深掘りボタン付き)
08:00  Daily News              tool_fetch_daily_news()  → 02_News/News_YYYY-MM-DD.md + デイリーノートに追記
```

`tools/tool_briefing_manager.py` の Provider Registry に登録されているのは
`Crypto` / `Obsidian` / `Schedule & Tasks` / `Gmail` の4つで、**ニュースは含まれていない**
(`tool_briefing_manager.py:126-140`)。

結果として:

- 07:30 の「Morning Briefing」は、その日のニュースを **一切見ないまま** 3項目を選定している。
  Gemini に渡る `raw_data` に News セクションが無いので、原理的に選びようがない。
- ニュースは30分後に別投稿で届く。ユーザーから見ると「朝の状況把握」が2回に分断される。
- 「最も重要な3項目」という Briefing の建て付けと、情報源にニュースが無い実態が食い違っている
  (CLAUDE.md が繰り返し潰してきた **表示と実態の食い違い** の一種)。

### 既に存在する重複 — ニュース要約の抽出が2実装ある

Briefing に News provider を足す前に、**ニュースの要約を作る場所が既に2つある**ことを
潰しておく。ここを見ずに3つ目を足すと、同じ要約を3箇所で別々に組み立てることになる。

| | 実装 | やり方 |
|---|---|---|
| A | `tools/tool_web.py:249-283` | ニュースツール自身が `> {summary}\n\n👉 [[…\|詳細はこちら]]` を NEWS_PLACEHOLDER に埋める。`summary` は Gemini から受け取った変数をそのまま使う |
| B | `tools/tool_daily_note.py:195-242` | 保存済みの News ノートを開き、`'要約'/'まとめ'/'概要'` を含む行を探して以降3行を拾い、**A とまったく同じ形の文字列**を作る (`tool_daily_note.py:237`) |

B は「手元にある変数」ではなく「保存された Markdown からの推測」で要約を復元している。
テンプレート (`Tpl_Permanent.md`) の見出し文言を変えると B だけが静かに壊れる。

さらに B には **lost update** がある。`generate_daily_note()` の流れ:

```
line 278  daily note を読む            content = (NEWS_PLACEHOLDER を含む)
line 298  _fetch_news_content()
            └ line 205 tool_fetch_daily_news()
                          └ tool_web.py:251 ノートを **ディスクから読み直して**
                            NEWS_PLACEHOLDER を埋め、tool_web.py:281 で **書き込む**
line 299  updated = content.replace(NEWS_PLACEHOLDER, ...)   ← 278 で読んだ古いコピー
line 305  updated をディスクへ書き込む                        ← A の書き込みを上書き
```

A が書いた内容は B に上書きされて消える。出力される文字列が A と B でほぼ同型なので
**見た目には気付けない** (残るのは B の再パース由来の要約)。

到達経路は `py tools/tool_daily_note.py` の手動実行のみ (スケジューラは
`include_news=False` で呼び、bot からは未接続) なので毎朝起きている事故ではないが、
`include_news` の既定値は `True` (`tool_daily_note.py:249`) であり、
呼び出しを1つ足せば表に出る。

**このブランチではサイドカー導入のついでに B を消す** — A/B/Briefing の3箇所が
同じサイドカーを唯一の出どころにすれば、要約の組み立ては1箇所になる。

### 範囲外 — 予定/タスクの3重実装 (別ブランチ)

同じ構造の重複が予定/タスク側にもある。`gws workflow +standup-report` を
`tool_daily_note._fetch_calendar_content` (`tool_daily_note.py:99`) と
`tool_calendar.tool_daily_summary` (`tool_calendar.py:351`) が別々に叩き、
meetings の整形ループは逐語的に同一 (`tool_daily_note.py:106-115` ≡ `tool_calendar.py:367-376`)。
さらに Daily Note 側はタスクを別コマンド (`gws tasks tasks list`) でもう1回取りに行くが、
`tool_daily_summary` は standup-report のレスポンスに既に入っている `tasks` を使うだけ。
3つ目の実装として `tool_calendar.tool_sync_to_daily_note` (Calendar API + Todoist) がある。

**このブランチでは触らない。** `tool_sync_to_daily_note` は Discord コマンド
(`commands/bot_commands_calendar.py:12`) と `lib/tool_registry.py:180` から参照されており、
統合すると Planner のツール選択にも影響する (`tests/test_answer_grounding.py:170-175` が
`tool_update_note` との弁別を検査している)。CLAUDE.md「1 session, 1 feature」に従い分ける。

## Goal

Morning Briefing の Gemini 選定に **その日のニュース** を情報源として供給する。
ただし **Gemini / Tavily の課金を増やさない** こと。ニュース取得はこれまで通り
`tool_fetch_daily_news()` が1日1回だけ行い、Briefing はその **成果物を読むだけ** にする。

あわせて、上記のニュース要約の二重実装 (B) を消し、**要約の出どころを1つにする**。

### 非目標 (このブランチではやらない)

- `tool_fetch_daily_news()` の記事収集ロジック・エディトリアル生成プロンプトの変更
- Discord の Morning Briefing 投稿レイアウト変更 (深掘りボタン等) — 本文に News が
  混ざる以上の変更はしない
- 08:00 Daily News の Discord 通知の廃止 (Vault への保存経路と通知はそのまま残す)
- 予定/タスクの3重実装の統合 (上記のとおり別ブランチ)

---

## 設計判断

### 判断1: Briefing は「取得」せず「読む」

`News` provider の中で `tool_fetch_daily_news()` を呼ぶ案 (= Briefing 1本に統合) は採らない。

| | 読むだけ (採用) | Provider 内で取得 |
|---|---|---|
| Gemini 呼び出し | 増えない | Briefing 側で +2回/日 (editorial + summary) |
| Tavily 呼び出し | 増えない | キーワード数 ×2 件/日 が Briefing の中で走る |
| Briefing 所要時間 | ほぼ不変 | Tavily の `time.sleep(1)` ×キーワード数 + Gemini2回ぶん延びる |
| 失敗の波及 | News が無ければ「未取得」と書いて続行 | ニュース取得の失敗が Briefing 全体を巻き込む |

`tool_generate_briefing()` は provider の例外を握って `## {name}\nエラー: {e}` に落とすので
(`tool_briefing_manager.py:224-225`) 全体が落ちはしないが、**Briefing の生成時間が
数分伸びるのは実害**。既存の `_build_xtrend_section()` (`tool_briefing_manager.py:150-195`)
が採っている「別ジョブが作った成果物を鮮度チェックして読む」形に揃える。

### 判断2: Markdown を再パースせず、JSON サイドカーを書く

News ノートは `Tpl_Permanent.md` テンプレートの
`このノートが言いたいことを1〜3行で書く。` を summary で置換して作られる
(`tools/tool_web.py:222-243`)。つまり **要約の位置がテンプレートの文面に依存している**。
ここを Briefing 側から正規表現で拾うと、テンプレートを1行直した瞬間に
Briefing の News セクションだけが無言で空になる — CLAUDE.md の
「リンクを出す側と解決する側は必ず対で直す」で挙げた `[[ファイル名#見出し]]` の事故と同型で、
**ログに一行も残らない失敗** になる。

そこで `tool_fetch_daily_news()` に、既に手元にある `summary` と見出しを
`02_News/latest_news.json` へ書き出す処理を足す (XTrend の `latest_features.json` と同じ作法)。
Briefing はこの JSON だけを読む。生成側と消費側が同じ構造体を介するので、
テンプレート変更に巻き込まれない。

```json
{
  "date": "2026-07-30",
  "summary": "…1〜3行の自然文…",
  "headlines": ["n8nに重大な脆弱性が発覚", "…", "…"],
  "note": "02_News/News_2026-07-30.md"
}
```

`headlines` は editorial 本文から `^### ` 行を拾って先頭N件。
本文全体 (数千字) を Briefing に流すと Gemini プロンプトが肥大し、
「3項目」の選定がニュースに引っ張られすぎるため、**要約 + 見出しだけ** に絞る。

### 判断3: 鮮度チェックは「今日の日付と一致するか」

XTrend は「2日以上前なら捨てる」だが、ニュースは1日ズレただけで古い。
`date != today` なら provider は `本日のニュースはまだ取得されていません。` を返す。
**古いニュースを今日のものとして Gemini に渡さない**ことを優先する
(Briefing は「今日は {today_str} です」と明言してプロンプトを組むため、
昨日のニュースを混ぜると Gemini が今日の出来事として書く)。

### 判断4: スケジュール順序 — News を Briefing の前に出す

読むだけの設計なので、**News が Briefing より先に走っていないと永久に空** になる。
現行の 08:00 → 07:30 の順序を入れ替える。

```
07:00  Daily Note Generation     (変更なし。News の追記先となるノートを先に作る)
07:10  Daily News                ← 08:00 から移動
07:30  Morning Briefing          (変更なし。この時点で latest_news.json が今日の日付になっている)
```

07:00 と同時刻にしない理由: `tool_fetch_daily_news()` はデイリーノートに追記する経路を持ち
(`tools/tool_web.py:249-283`)、ノートが未作成だと `Daily note not found (skipped link)` になる。
デイリーノート生成の完了を待たせるため10分空ける。

### 判断5: lost update は「読み直す」で消す。ニュースツール側の書き込みは残す

`tool_fetch_daily_news()` がデイリーノートを埋める経路 (A) は残す。
スケジューラの単独ジョブ・`trigger_daily_news.py`・Discord の `/news` からも呼ばれ、
そのとき `generate_daily_note()` は関与しないため、A を消すとそれらの経路で
NEWS_PLACEHOLDER が埋まらなくなる。

代わりに `generate_daily_note()` 側を **「ニュースツールを走らせたら必ず読み直す」** に直す。
`tool_fetch_daily_news()` を呼んだ後にディスクからノートを読み直せば、
その時点で NEWS_PLACEHOLDER は A によって既に埋まっており、
`generate_daily_note()` が自分で埋める処理は**そもそも走らない**。
read-modify-write が1往復になるので、上書きが構造的に起こらなくなる。

A が失敗した場合 (`No news found for today.` / ノート未作成でスキップ) は
プレースホルダが残るので、そのときだけサイドカーから埋めるフォールバックを残す。
**ヒューリスティックな Markdown 再パース (B) はここで削除する。**

---

## Steps

1. **`tools/tool_web.py`: サイドカー書き出しを追加**
   `tool_fetch_daily_news()` の保存処理の直後に `_write_news_sidecar(today_str, summary, editorial, output_filename)` を追加。
   見出し抽出は `editorial` の `^### ` 行から先頭5件。
   → 期待結果: News 実行後に `02_News/latest_news.json` が今日の日付で生成される。
   → サイドカー書き出しの失敗は **ニュース保存本体を巻き込まない** (try/except でログのみ)。
      ニュースノートは既に保存済みなので、ここで例外を投げると成功が失敗に化ける。

2. **`tools/tool_briefing_manager.py`: `get_news_summary()` provider を追加**
   `_NEWS_JSON = os.path.join(WORK_DIR, NEWS_DIR_NAME, "latest_news.json")` を読み、
   `date == today` なら `summary` + `headlines` の箇条書きを返す。
   ファイル無し / JSON 壊れ / 日付ズレ → `本日のニュースはまだ取得されていません。`
   `add_provider("News", get_news_summary)` を `Crypto` / `Obsidian` の隣に置く (無条件登録)。
   → 期待結果: `_providers` が5つになり、`raw_data` に `## News` セクションが載る。

3. **`agent_scheduler.py`: Daily News を 08:00 → 07:10 に移動**
   `SCHEDULE` の `"time": "08:00"` を `"07:10"` に変更 (コメントで「Briefing より前に
   走らないと News provider が空になる」理由を明記)。
   → 期待結果: 07:10 News → 07:30 Briefing の順で走る。

4. **`verify_project.py`: `run_briefing_check()` の期待リストに `News` を追加**
   `expected_providers = ['Crypto', 'Obsidian', 'Gmail']` → `+ 'News'`。
   News provider は Gmail と違い条件付き登録ではないので、常に存在すべき。
   → 期待結果: provider の登録が消えたら Guardian が赤くなる。

5. **`tools/tool_daily_note.py`: 二重実装 (B) の削除と lost update の解消**
   - `_fetch_news_content()` の Markdown 再パース (`tool_daily_note.py:212-235`) を削除し、
     サイドカーの `summary` を読む形に置き換える。取得ロジックはステップ2で書く
     `get_news_summary()` とは**別**にする — provider は Gemini に渡す素材
     (要約 + 見出し) を返し、こちらは Obsidian に埋める整形済み Markdown
     (`> summary` + `[[…\|詳細はこちら]]`) を返すため、戻り値の用途が違う。
     共有するのは**サイドカーを読む1関数** (`_read_news_sidecar()`) にとどめる。
   - `generate_daily_note()`: ニュースツールを呼んだ後にノートを読み直す (判断5)。
   → 期待結果: 要約の文言が A と B で食い違わなくなる。
     `py tools/tool_daily_note.py` を実行しても A の書き込みが消えない。

6. **`tests/test_news_briefing_provider.py` を追加** (下記 Verification 参照)

---

## Verification

**前提: この計画のテストは実 API を1回も叩かない。** サイドカーの読み書きと provider の
分岐だけを対象にし、`tool_fetch_daily_news()` 本体 (Tavily/Gemini) は呼ばない
(CLAUDE.md「テストを一周回すだけで課金や実副作用が起きてはいけない」)。
Vault への書き込みも行わず、`tmp_path` 相当の一時ディレクトリへ `_NEWS_JSON` を差し替える。

- [ ] **[1] サイドカー生成**: `_write_news_sidecar()` に既知の editorial を渡し、
      `date` / `summary` / `headlines` (`### ` 行が順序どおり最大5件) が入ることを確認
- [ ] **[2] provider 正常系**: 今日の日付の JSON を置き → `get_news_summary()` が
      summary と全 headline を含む文字列を返す
- [ ] **[3] provider 鮮度**: 昨日の日付の JSON を置き → 「まだ取得されていません」を返し、
      **昨日の summary 本文を含まない** (逆方向の確認。含んでいたら古い記事が今日として流れる)
- [ ] **[4] provider 異常系**: ファイル無し / 壊れた JSON / `date` 欠落 の3ケースで
      例外を投げずに未取得メッセージを返す (Briefing 全体を落とさない)
- [ ] **[5] 登録の確認**: `from tools.tool_briefing_manager import _providers` に
      `"News"` が居る (Guardian と同じ判定をテスト側にも置き、片方だけ直る事故を防ぐ)
- [ ] **[6] スケジュール順序の静的検査**: `agent_scheduler.SCHEDULE` を読み、
      `Daily News` の `time` が `Morning Briefing` の `time` より **前** であることを assert。
      時刻をどちらか片方だけ動かすと赤くなる — この設計の生命線が「順序」なので
      コメントではなくテストで縛る
- [ ] **[7] lost update の回帰**: 一時ディレクトリにデイリーノート (3つのプレースホルダ入り)
      を置き、`tool_fetch_daily_news` を **ノートへ既知の文字列を書き込むスタブ** に
      差し替えて `generate_daily_note(include_news=True)` を実行 →
      保存後のノートに **スタブが書いた文字列が残っている** ことを assert。
      現行コードでこのテストを走らせると落ちる (= 修正前に赤いことを先に確認する)
- [ ] **[8] 再パースが消えたことの静的検査**: `tools/tool_daily_note.py` の
      ソースに `'まとめ'`/`'概要'` によるヒューリスティック抽出が残っていないことを
      ast で確認。B を消したつもりで別関数に残る事故を止める
- [ ] 各テストは `sys.exit(1 if _FAIL else 0)` まで通す (CI が終了コードで判定するため)
- [ ] `py verify_project.py` (フル) が緑 — [6] Briefing Providers に `News` が出ること
- [ ] `py tests/test_news_briefing_provider.py` が単体で緑

### 手動確認 (実機・任意)

- [ ] `py trigger_daily_news.py` → `02_News/latest_news.json` が生成される
- [ ] `py -c "from tools.tool_briefing_manager import get_news_summary; print(get_news_summary())"`
      → 今日の要約が出る
- [ ] `/briefing` を Discord で叩き、本文に News 由来の項目が混ざることを確認

---

## Rollback

4ファイル + テスト1本の追加で、いずれも既存の呼び出し規約を変えない。

- `git revert` 1コミットで元に戻る
- 部分的に戻す場合:
  - provider だけ外す → `add_provider("News", ...)` の1行と `verify_project.py` の
    `'News'` を消せば、Briefing は従来の4 provider に戻る
  - スケジュールだけ戻す → `07:10` を `08:00` に戻す。この場合 News provider は
    毎朝「まだ取得されていません」を返すが、**Briefing は落ちない** (判断3の設計)
- `02_News/latest_news.json` は追加ファイルなので、残っても既存の読み手はいない
- ステップ5 (`tool_daily_note.py`) だけ唯一「削除を伴う」変更なので、revert 時は
  ここが元に戻ることを確認する。ただし削除するのは**再パースのヒューリスティック**だけで、
  `_fetch_news_content()` の関数名・シグネチャ・戻り値の形は変えないため、
  呼び出し側 (`generate_daily_note`) 以外への影響はない

---

## 影響範囲

| ファイル | 変更 | 種別 |
|---|---|---|
| `tools/tool_web.py` | `_write_news_sidecar()` 追加 + 呼び出し1行 | 追加のみ |
| `tools/tool_briefing_manager.py` | `get_news_summary()` + `add_provider("News", ...)` | 追加のみ |
| `agent_scheduler.py` | Daily News の時刻 08:00 → 07:10 | 1行 |
| `verify_project.py` | `expected_providers` に `'News'` | 1行 |
| `tools/tool_daily_note.py` | 再パース削除 + 読み直し | **削除を含む** |
| `tests/test_news_briefing_provider.py` | 新規 | 追加のみ |

`main.py` の TOOLS レジストリ、`lib/tool_registry.py` のツール説明文、Discord コマンドは変更なし
(公開ツールの数も引数も変わらないため)。`tool_sync_to_daily_note` は今回触らない。

## 完了後に残る「ニュース要約の出どころ」

```
tool_fetch_daily_news()  ← Gemini から summary を受け取る唯一の場所
        │
        ├─ News ノート (02_News/News_YYYY-MM-DD.md)     ← 人が読む
        ├─ デイリーノートの NEWS_PLACEHOLDER            ← A (従来どおり)
        └─ 02_News/latest_news.json                     ← 新設。機械が読む
                 │
                 ├─ tool_briefing_manager.get_news_summary()   (Briefing の素材)
                 └─ tool_daily_note._fetch_news_content()      (A が失敗したときのみ)
```

要約の文字列を**生成する**のは `tool_fetch_daily_news()` の1箇所だけになる。
下流はすべて「受け取って整形する」側に揃う。
