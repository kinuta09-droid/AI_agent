# Google Search Grounding 導入計画 — 精度優先の検索ツールを追加する

## 背景 (Context)

2026-08-03、「今日急にガソリンが5円値下がりした理由」という質問で、Gemini(消費者版)は
7/30の補助金単価改定(28.2円/L)という正確な一次情報を回答したのに対し、ARMSの
`/agent` は誤答・不十分な回答を繰り返した。同日中に3段階で `tools/tool_web.py` の
`tool_web_search` (Tavily) を直した:

1. `recency_days` 追加 (一般検索+news検索のマージ) — PR #46
2. news結果を先頭に+結果ごとのcontent切り詰め (下流の1500字カットで消えるのを防止) — PR #48
3. `recency_days` 指定時に検索クエリへ今日の年月を含めるガイド追加 (Tavilyのランキングが
   クエリの言い回しに敏感なため) — PR #49

3段階とも実測で改善を確認したが、それでも**Tavilyの検索ランキングがクエリの
微妙な言い回し差で変動し、狙った一次情報を安定して拾えないケースが残った**
(全く同じ質問文でもPlannerが生成するクエリの語順・キーワード次第で結果が変わった)。
Geminiが安定して強いのは、Google自身のSearch Grounding機能(Google検索のインデックスに
直接アクセスする仕組み)を使っているためと考えられる。

本計画は、このプロジェクトが既に使っている `google.genai` クライアント (`lib/lib_shared.py`)
に同じGrounding機能を追加導入し、**精度が特に重要な「直近の出来事」系の質問**で
選べるようにするもの。**既存の `tool_web_search` (Tavily) は置き換えない** —
無料枠が大きく3段階の改善で機能しているので、選択肢を増やす形にする。

## 進捗表 — 中断・再開はここを見る

| # | 内容 | 状態 | 実施日 | 備考 |
|---|---|---|---|---|
| 0 | 実機でAPIを1回叩き `response` の実構造を確認 | 未着手 | | **最優先・ここを飛ばして実装しない** |
| 1 | `lib/cost_guard.py` に `GROUNDING_COUNTER` 追加 | 未着手 | | |
| 2 | `tool_web_search_grounded` 実装 | 未着手 | | |
| 3 | `lib/tool_registry.py` 登録 | 未着手 | | |
| 4 | Planner (`engine_plan_execute.py`) の使い分けガイド追加 | 未着手 | | |
| 5 | テスト (`tests/test_tool_web_grounded.py`) | 未着手 | | |
| 6 | 実機比較検証 | 未着手 | | |
| 7 | クローズ処理 (テスト→Guardian→PR→マージ) | 未着手 | | |

状態は `未着手` / `進行中` / `完了` / `見送り(理由)` で更新する。

---

## 設計方針 (このセッションで確定済み — 推測で書き直さないこと)

### SDK呼び出し
context7 (`/googleapis/python-genai`) で確認済み:

```python
from google.genai import types

response = client.models.generate_content(
    model=MODEL_NAME,  # lib_shared の LIGHT モデル (gemini-3.1-flash-lite-preview)
    contents=query,
    config=types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    ),
)
```

`client` / `MODEL_NAME` はどちらも `lib/lib_shared.py` から既存importでそのまま使える。

### 引用元の取り出し方 — ★次セッションの最初に実機で確定させること★

`GroundingChunkWeb` 型 (`title` / `uri` / `domain` を持つ) の存在はcontext7の型定義で
確認済みだが、`response` からそこへ至る実際の属性パス
(`response.candidates[0].grounding_metadata.grounding_chunks[i].web` になる見込みだが
ドキュメント断片からは全体像が確定できなかった)・`grounding_metadata` が
`None` になるケースの扱いは**未検証**。

**Step 0 として、実装に入る前に必ず以下を実機で実行し、実際の構造を確認すること**:
```python
from lib.lib_shared import client, MODEL_NAME
from google.genai import types

response = client.models.generate_content(
    model=MODEL_NAME,
    contents="今日のガソリン価格のニュースを教えて",
    config=types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    ),
)
print(response.text)
print(response.candidates[0].grounding_metadata)
```
ここで得た実際の属性パスを元に、URL一覧を安全に取り出すヘルパー
(`grounding_metadata` が None / `grounding_chunks` が空リストの場合のガードを含む)
を書く。**ここを推測のまま実装しない。**

### コスト管理

`lib/cost_guard.py` の `WATCHER_COUNTER` / `SWEEP_COUNTER` ([lib/cost_guard.py:179](lib/cost_guard.py:179)
[lib/cost_guard.py:192](lib/cost_guard.py:192)) と全く同じパターンで `GROUNDING_COUNTER` を追加する:

```python
GROUNDING_COUNTER_PATH = os.path.join(LOGS_DIR, "grounding_daily_calls.json")
GROUNDING_COUNTER = DailyCounter(
    path=GROUNDING_COUNTER_PATH,
    limit=get_limit("DAILY_GROUNDING_CALL_LIMIT", 30),
    name="Google Search Grounding",
)
```

デフォルト30/日は「無料枠(後述)に対する安全弁」であって実際の利用量の見込みではない
(想定利用は1日数回程度)。

**料金メモ (2026-08-03 WebSearchで確認、実装前に [ai.google.dev の最新料金表](https://ai.google.dev/gemini-api/docs/pricing)
で再確認すること — 変わっている可能性がある)**:
- LIGHT モデル (`gemini-3.1-flash-lite-preview`) は Gemini 3ファミリーのため、
  無料枠は **5,000プロンプト/月 (Gemini 3ファミリー全体で共有)**、超過分は
  **$14 / 1000検索クエリ**。他の3.x系呼び出しと共有の月次枠である点に注意
  (現在の全Gemini呼び出しは1日100件程度・月3000件程度なので、grounding分を
  足しても当面は無料枠に収まる見込み)

### 例外処理 — 特別扱い不要

`GeminiBudgetExceeded.__str__` が完成した日本語文を返す設計 ([lib/cost_guard.py:38](lib/cost_guard.py:38))
なので、`tool_web_search` と同じ広い `try: ... except Exception as e: return f"Error: {e}"`
で自然に「Error: ⛔ 本日の...」という、このプロジェクトの失敗プレフィックス規約に
沿った文字列に落ちる。個別のexcept節は不要。

### ツールの使い分け (Tavily版 vs Grounding版)

- `tool_web_search` (Tavily): 既定・無料枠が大きい・一般的な調べ物
- `tool_web_search_grounded` (新規): 「今日/本日/急に」等の直近性が特に重要で、
  精度がずれると答えの意味が無くなる質問向け。具体的な使い分けルールは
  ステップ4で `engine_plan_execute.py` の Planner ガイド (719行目付近、
  `recency_days` のガイドと同じ並び) に追記する。候補案:
  - 最初から `tool_web_search_grounded` を選ばせる (シンプルだが、Tavilyで
    十分なケースにも余計なGemini課金が乗る)
  - まず `tool_web_search` (Tavily, `recency_days`付き) を試し、Criticが
    `needs_revision` と判断した場合の再検索候補として `tool_web_search_grounded`
    を使う (既存のCritic再試行フローに乗るので実装は小さいが、1往復遅くなる)

  どちらを採るかは実装セッションで実測しながら決めてよい (後者の方が
  コスト面では手堅いが、まずは前者で実装してから比較する手もある)。

---

## Steps (詳細)

1. **Step 0 (最優先)**: 上記の実機確認スクリプトを実行し、`grounding_metadata` から
   URL一覧を取り出す正確なコードを確定させる
2. `lib/cost_guard.py` に `GROUNDING_COUNTER` を追加 (上記コード)
3. `tools/tool_web.py` に `tool_web_search_grounded(query: str) -> str` を追加
   (同ファイル内の `tool_web_search` の直後が自然。ファイルが肥大化するようなら
   新規 `tools/tool_web_grounded.py` に分けてもよい)。出力フォーマット案:
   ```
   {response.text}

   [参照元 (Google検索)]
   - {title1}: {uri1}
   - {title2}: {uri2}
   ```
   引用元リストは `tool_web.py` の `_RESULT_CONTENT_CHARS` と同じ考え方で
   件数を絞る (下流の1500字切り詰めで消えないように)
4. `lib/tool_registry.py` に登録。`description` に Tavily版との使い分けを明記する
   (例: `"Web検索(Google Search Grounding、精度優先・直近の出来事向け。一般的な
   調べ物はtool_web_searchを使う)"`)
5. `engine_plan_execute.py` のツール選択ガイド・`_resolve_tool_args` に
   使い分けルールを追記 (上記「ツールの使い分け」節で決めた方式)
6. `tests/test_tool_web_grounded.py` 新規:
   - `google.genai` クライアントをスタブ (`tests/test_tool_web_recency.py` や
     `tests/test_curate_note_save.py` の `_FakeGenaiClient`/`_FakeModels` パターンを流用)
   - 正常系: `grounding_metadata` に複数URLがある場合に整形されること
   - 異常系: `grounding_metadata` が None / `grounding_chunks` が空リストの場合に
     例外にならず本文のみ返ること
   - `GROUNDING_COUNTER` 連携: 上限到達時に `Error: ⛔...` が返ること
   - `scripts/run_test_sweep.py` の `PARALLEL_TESTS` に登録
7. 実機で「今日急にガソリンが5円値下がりした理由」を新ツールで再質問し、
   Gemini(消費者版)の回答と並べて精度を比較。ついでに一般的な質問(recency不要)で
   `tool_web_search` が引き続き選ばれることも確認 (新ツールに全部奪われないか)
8. `py scripts/run_test_sweep.py` / `py verify_project.py` で最終確認
9. コミット → ブランチ → PR → マージ ([close-out-procedure](../../.claude/CLAUDE.md) の通常フロー)

## Verification
- [ ] Step 0 の実機確認が完了し、推測箇所が残っていない
- [ ] `tests/test_tool_web_grounded.py` 全PASS (異常系含む)
- [ ] `py scripts/run_test_sweep.py` / `py verify_project.py` 緑
- [ ] 実機比較でGrounding版の回答がGemini(消費者版)相当の具体性を持つことを確認
- [ ] 一般的な質問で `tool_web_search` (Tavily) が引き続き選ばれ、新ツールに
      置き換わっていないことを確認 (コスト面の回帰防止)

## Rollback
新規ファイル/新規関数の追加が中心で既存の `tool_web_search` は無変更。
該当コミット(群)を `git revert` すれば即座に元に戻る。
