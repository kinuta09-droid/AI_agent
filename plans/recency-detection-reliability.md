# 検索の「直近性」判断を確実にする — コード側ガード + 手動リトライボタン

## 背景 (Context)

2026-08-03、ガソリン価格の質問を巡って4回連続で `/agent` に👎が付いた。都度コードを直した
(recency_days追加 PR #46 → 切り詰め順序修正 PR #48 → クエリへ年月付与 PR #49 →
Google Search Grounding版ツール追加 PR #51)が、直した機能自体は動くものの、
**「今日/急にを検知してrecency_daysを付ける」「精度重視ならgrounded版を使う」という
判断そのものがPlanner(LLM)の確率的な決定**であるため、同じような言い回しの質問でも
実行のたびに適用されたりされなかったりした。実際、PR #49・#51 のどちらも
マージ直後の実行でガイドが無視され、また同じ古い記事に基づく回答に戻っている。

プロンプトで「お願いする」アプローチは既に3回試して再現性が上がりきらないと分かったため、
本計画では2段構えにする:

1. **コード側の決定的ガード**: 「今日/急に/直近」等のキーワードをプロンプトではなく
   Pythonコードで検知し、LLMの判断を経由せず`recency_days`と年月クエリを強制付与する
2. **手動リトライボタン**: それでも外れた場合の保険として、`/agent` の回答に付いている
   👍👎ボタンと同じ場所に「🕐 最新情報で再検索」ボタンを追加し、クリック1つで
   `tool_web_search_grounded`(Google Search Grounding、既にPR #51で実装済み)を
   直接叩いて再検索できるようにする

## 進捗表 — 中断・再開はここを見る

| # | 内容 | 状態 | 実施日 | 備考 |
|---|---|---|---|---|
| 1 | `engine_plan_execute.py` にキーワード検知+強制付与ロジック追加 | 完了 | 2026-08-04 | `_RECENCY_KEYWORDS`/`_has_recency_intent`/`_current_year_month` 追加、`_run_step` の args else節に強制付与を挿入 |
| 2 | `commands/bot_views.py` に「🕐最新情報で再検索」ボタン追加 | 完了 | 2026-08-04 | `AgentFeedbackView.recency_retry` 追加。`_finish()`は呼ばず単独disabled |
| 3 | 強制付与ロジックの単体テスト | 完了 | 2026-08-04 | `tests/test_recency_guard.py` 新規作成 (17件PASS)。キーワード有無・上書き禁止・二重付与禁止の3パターン網羅 |
| 4 | ボタンの回帰テスト (`tests/test_agent_feedback.py`) | 完了 | 2026-08-04 | `recency_retry` の2件追加 (単独disabled化+失敗アイコン)、全13件PASS |
| 5 | テスト全体+Guardian確認 | 完了 | 2026-08-04 | `test_recency_guard.py`をPARALLEL_TESTSに登録。スイープ85ファイル全PASS(126.3s)、`verify_project.py`緑 |
| 6 | 実機比較検証 | 完了 | 2026-08-04 | 実機で確認: `tool_web_search`にrecency_days+年月付与が機能(ノート作成まで完走)、🕐ボタンも実クリックで無事故動作(`tool_web_search_grounded`直接呼び出し・エラーログなし)。ただし`_has_recency_intent`の強制付与分岐そのものは今回の実行では未発火(条件不成立)——その分岐は単体テスト17件で決定的に検証済み |
| 7 | クローズ処理 (テスト→Guardian→PR→マージ) | 進行中 | 2026-08-04 | |

状態は `未着手` / `進行中` / `完了` / `見送り(理由)` で更新する。

---

## 調査で確認済みの事実 (このセッションで確認済み — 推測で書き直さないこと)

- `execute_plan(plan, step_callback=None)` は `user_query` を受け取らない。ガードは
  各ステップの `task` 文字列(Plannerが生成した説明文)を見るしかないが、実際の4件の
  失敗ログでは `task` に「急に」「急低下」等のキーワードが確認できており、実用上十分な
  シグナルになっている。
- 挿入点は `engine_plan_execute.py:474` `args = _resolve_step_args(...)` の
  成功直後、`observe()` 呼び出し(`engine_plan_execute.py:495`)の前
  (`_run_step` 内、482行目の `else:` ブロック)。
- `tool_web_search_grounded(query: str) -> str` は既に `tools/tool_web.py:89` に
  実装済み(PR #51)。本文+参照元リストの文字列を返し、内部で `GROUNDING_COUNTER` の
  日次上限チェックも行う。例外は全て `Error: ...` 文字列に落ちる。
- `AgentFeedbackView` (`commands/bot_views.py:255`) が👍👎ボタンの実体。
  `self._goal`(元の質問文字列)・`self._result`(前回の回答)をインスタンス状態として持つ。
  `timeout=3600`(有限、永続化なし)。ボタンは `@discord.ui.button` デコレータ形式。
  👍/👎(モーダル経由)はどちらも共通の `_finish()` を呼び、`self.children` を**全て**
  disabled にする。
- ビューの生成・添付は `send_feedback_prompt(channel, goal, result)`
  (`commands/bot_commands_agent.py:181`) 一箇所に集約されており、
  `/agent`(`commands/bot_commands_agent.py:255`)・
  `/chat agent`(`bot_discord.py:581`)の両方から呼ばれる。
  **ここを直せば両entry pointに自動で反映される**(新しいViewを作るだけで済み、
  呼び出し側の変更は不要)。
- 既存テスト `tests/test_agent_feedback.py` に👍👎の回帰テスト一式が既にある
  (`_make_fake_interaction` / `save_feedback` のモンキーパッチ手法)。新ボタンのテストも
  同じ手法を踏襲する。

## 設計方針

### 1. コード側ガード (engine_plan_execute.py)

`_run_step` 内、`tool_name == "tool_web_search"` の場合に以下を**LLMの判断を待たず**適用する:

```python
_RECENCY_KEYWORDS = ("今日", "本日", "急に", "直近", "たった今", "さっき", "さきほど")

def _has_recency_intent(text: str) -> bool:
    return any(kw in text for kw in _RECENCY_KEYWORDS)

def _current_year_month() -> str:
    now = datetime.datetime.now()
    return f"{now.year}年{now.month}月"
```

- `recency_days` が未設定 かつ `_has_recency_intent(task)` → `args["recency_days"] = 10` を強制付与
- `args.get("recency_days")` が真 かつ `_current_year_month()` が `args["query"]` に含まれない
  → クエリ末尾に年月を追記 (LLMが自分で正しく付けていた場合は二重付与しない)

**位置づけ**: 既存のプロンプト指示 (719-732行目・219-223行目・899-901行目) は削除しない。
それらは「キーワードに引っかからない曖昧な直近質問」も拾える可能性があるため残し、
今回追加するコードガードは**プロンプトが機能しなかった場合の決定的な下限**として働く
(prompt指示とのAND/ORではなく、コード側が「未設定なら埋める」形なのでLLMが正しく
判断した場合は何もしない=競合しない)。

`tool_web_search_grounded` への自動切り替えはコード側では行わない(無料枠が大きい
Tavily を既定のまま保ち、Grounding の課金は「手動ボタン」または既存のCritic再検索
ガイド (899-901行目) 経由に限定する、という既存のコスト方針を維持する)。

### 2. 手動リトライボタン (commands/bot_views.py)

`AgentFeedbackView` に3つ目のボタンを追加する(新規Viewクラスは作らない — 同じ質問・
回答に対する操作なので既存クラスに載せるのが自然):

```python
@discord.ui.button(label="🕐 最新情報で再検索", style=discord.ButtonStyle.secondary)
async def recency_retry(self, interaction: discord.Interaction, button: discord.ui.Button):
    button.disabled = True  # このボタンだけ無効化 (👍👎は独立に残す — _finish()は呼ばない)
    await interaction.response.edit_message(view=self)

    from tools.tool_web import tool_web_search_grounded
    result = await asyncio.to_thread(tool_web_search_grounded, self._goal)

    from lib.bot_errors import is_failure_result
    icon = "❌" if is_failure_result(result) else "🕐"
    await interaction.followup.send(f"{icon} 最新情報で再検索した結果:\n\n{result}")
```

- `_finish()` は呼ばない(👍👎の「評価」とは別の操作のため、全ボタンを道連れにしない)。
  ただし👍/👎側で先に `_finish()` が呼ばれれば `self.children` に含まれるこのボタンも
  一緒にdisabledになる(想定通りの挙動)。
- 常に表示する(元の質問が検索向きかどうかで出し分けない)。理由: `send_feedback_prompt`
  は `goal`/`result` の文字列しか受け取らず、どのツールが使われたかの情報を持っていない。
  それを持たせるには `run_plan_and_execute` の戻り値やシグネチャを拡張する必要があり
  スコープが膨らむ。ボタン名で用途が明示されているため、的外れな質問では単に押されない
  ことを期待する(過剰設計を避ける)。
- Discordの3秒応答制限は `interaction.response.edit_message(...)` で満たし、実際の検索は
  `followup.send(...)` 側で行う(`tool_web_search_grounded` は数秒かかりうるため)。

## Steps (詳細)

1. `engine_plan_execute.py`: `_RECENCY_KEYWORDS` / `_has_recency_intent` / `_current_year_month`
   を追加し、`_run_step` の該当箇所に強制付与ロジックを挿入
2. `commands/bot_views.py`: `AgentFeedbackView` に `recency_retry` ボタンを追加
3. `tests/test_answer_grounding.py` または新規テストに、`_has_recency_intent` /
   強制付与ロジックの単体テストを追加 (キーワードあり/なし、既にrecency_days設定済み
   の場合は上書きしない、既に年月が入っている場合は二重付与しない、の3パターン)
4. `tests/test_agent_feedback.py` に以下を追加 (既存の `test_agent_feedback_good_button_*`
   と同じ `_make_fake_interaction` / モンキーパッチの手法を踏襲):
   - `recency_retry` が `tool_web_search_grounded(self._goal)` を呼ぶこと
   - クリック後、**このボタンのみ** disabled になり、👍👎は enabled のまま残ること
   - `tool_web_search_grounded` が `Error:` を返した場合に ❌ アイコンになること
5. `py scripts/run_test_sweep.py` / `py verify_project.py` で最終確認
   (新規テストは `scripts/run_test_sweep.py` の `PARALLEL_TESTS` への登録を忘れないこと)
6. 実機で同じガソリン価格の質問を再現し、(a) 何もせず自動でrecency_days+年月が
   付くこと、(b) ボタンを押して再検索が機能することの両方を確認
7. コミット → ブランチ → PR → マージ (通常のクローズ処理)

## Verification
- [ ] 新規/既存テスト全PASS (`test_answer_grounding.py` / `test_agent_feedback.py` / スイープ全体)
- [ ] `py verify_project.py` 緑
- [ ] 実機: 「今日急に○○」系の質問でログに `recency_days` が必ず付くことを確認
      (キーワード検知なのでLLMの気分に左右されない)
- [ ] 実機: 「🕐 最新情報で再検索」ボタンを押し、`GROUNDING_COUNTER` がインクリメントされ
      新しい回答がchannelに投稿されることを確認

## Rollback
`engine_plan_execute.py` の追加は既存ロジックに割り込む形(未設定時のみ埋める)なので、
コミットをrevertすれば即座に元の(prompt頼みの)挙動に戻る。`bot_views.py` のボタン追加も
既存クラスへの追記のみで、他の2ボタンの挙動には影響しない。
