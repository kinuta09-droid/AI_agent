# Engagement Log — Phase 2 への地ならし

> 作成: 2026-07-29 / 対象ブランチ: `claude/multi-agent-rag-monetization-m1a7mx`
> 帰宅後にPCで上から順に実行できるように書いてある。**Step 0 を必ず最初にやること。**

---

## 背景 — なぜ今これをやるのか

`lib/engagement_log.py` は Phase 1 (収集のみ) で 2026-07-25 に着地し、そのまま
検索品質トラック (Phase A2 → B → D → C) に移って4日間戻ってきていない。
「時間を空けたほうが効果が高い」という判断は **git 履歴・CLAUDE.md・plans/ の
どこにも記録が無い**。ただし待つ根拠自体は実在する:

```
Phase 1 バックフィル実投入 354件 = query 293 / note_created 15 / open seed 46
                                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                    impression と click がゼロ
```

Phase 2 の中核信号 CTR = click / impression は **「何を見せたか」を過去ログから
復元できない**ため、07-25 のライブ記録開始以降にしか存在しない。`/curate` は
一度に3件しか出さないので、毎日使っていても impression は 1日12件のオーダー。
この量でスコアラを組んでも学習対象はノイズになる。

**したがって、待つ必要があるのはスコアラだけ。** 収集口を増やす作業は
待つほど損をする (待機期間ぶんの信号がそのまま失われる)。

| 作業 | データ待ちが要るか | 本プランの扱い |
|---|---|---|
| `EVENT_WEIGHTS` を使うスコアラ | **要る** (CTR 待ち) | Step 3 で出口条件だけ決める |
| `/taste` (`summarize()` を Discord に出す) | 不要 | **Step 1** |
| `edit` イベントの配線 (重み+3・現在ゼロ) | 不要・早いほど良い | **Step 2** |
| `note_created` のライブ経路 | 不要・同上 | Step 2 に同梱 |

`edit` は `EVENT_WEIGHTS` に `+3` (`query` +5 に次ぐ2番目の強さ) で定義されて
いるのに発火箇所がゼロ。`note_created` は `scripts/backfill_engagement.py:159`
のバックフィル経路しか無く、ライブでは1件も記録されていない。

---

## Goal

1. 溜まっている信号を Discord から見えるようにする (`/taste`)
2. 取りこぼしている2イベント (`edit` / `note_created`) の収集を開始する
3. スコアラ着手の**数値的な出口条件**を決めて、判断を勘から外す

**やらないこと**: スコアラ本体、ランキングへの反映、`/curate` の提案ロジック変更。
Phase 1 と同じく「挙動不変・観測のみ追加」を保つ。

---

## Step 0 — 現状把握 (最初に必ず / 所要2分)

```bash
cd C:\AI_agent
py lib/engagement_log.py 90
```

**見るべき数字は3つ:**

- `### イベント種別` の `impression` と `click` の実数
- `### CTR (click / impression)` の `curate` 行
- `### イベント種別` に `edit` が無いこと (= Step 2 の前提確認)

この出力を**そのままメモしておく** (Step 3 の判断材料 + Step 2 の前後比較の before)。
`impression がまだありません` と出た場合は `/curate` がこの期間一度も使われて
いないということなので、Step 3 の出口条件は自動的に「まず `/curate` を使う」になる。

> クラウド側からは `logs/` が見えないため、この数字は実機でしか取れない。
> Step 1/2 の実装自体はこの数字に依存しないので、数字が期待外れでも先に進んでよい。

---

## Step 1 — `/taste` (表示のみ / 所要30分・変更2ファイル)

### 目的
`summarize()` は既に完成しているのに CLI 専用で、`py lib/engagement_log.py 30` を
手で叩かないと誰も見ない。**新しいロジックは1行も書かない。表示経路だけ足す。**

### 実装

**1-a. `commands/bot_commands_tools.py` に `cmd_taste` を追加**

`cmd_memory` (同ファイル L84-101) が**完全に同じ形**なので、それを雛形にする:
`defer()` → 集計取得 → `_send_with_fallback`。

```python
async def cmd_taste(interaction: discord.Interaction, days: int = 30):
    """エンゲージメント信号の収集状況を表示する (閲覧のみ・Phase 1 の可視化)。"""
    await interaction.response.defer()
    try:
        from lib.engagement_log import summarize
        result = summarize(days=days)
    except Exception as e:
        result = log_command_error("/taste", e)

    if _send_with_fallback:
        await _send_with_fallback(interaction, result, title="Engagement", emoji="📊")
    else:
        await interaction.followup.send(result[:2000])
```

**1-b. `bot_discord.py` にスラッシュコマンドを登録**

L969 の `cmd_memory` 登録の直後に、同じ形で追加する:

```python
@tree.command(name="taste", description="エンゲージメント信号の収集状況（CTR・頻出クエリ・よく開くノート）")
@app_commands.describe(days="集計対象の日数（デフォルト: 30）")
async def cmd_taste(interaction: discord.Interaction, days: int = 30):
    await bot_commands_tools.cmd_taste(interaction, days)
```

### 遵守事項 (Known Bug Patterns より)

- ❌ `except Exception as e: result = f"Error: {e}"` と書かない → **必ず `log_command_error("/taste", e)`**
  (bot層の握りつぶしは `verify_project.py` の `check_swallowed_exceptions()` が落とす)
- ❌ 見出しに `✅` をハードコードしない。今回は成否判定をしないので絵文字は `📊` 固定でよい
- ⚠️ `summarize()` は 2000字を超えうる (頻出クエリTOP10 + よく開くノートTOP10)。
  `_send_with_fallback` はこれを扱えるはずだが、**Step 1 の検証で長い出力を必ず一度通す**

### 検証

```bash
py verify_project.py            # ALL SYSTEMS GO を確認
scripts\arms.bat restart
```

Discord で:
- [ ] `/taste` が候補に出る (出なければコマンド同期待ち。数分待つか Bot 再起動)
- [ ] 出力が `py lib/engagement_log.py 30` と**一致する**
- [ ] `/taste days:90` が Step 0 の出力と一致する
- [ ] 長文が切れずに送信される (2000字超で fallback 経路に入ることを確認)

### コミット

```bash
git add commands/bot_commands_tools.py bot_discord.py
git commit -m "feat(personalization): /taste でエンゲージメント収集状況を可視化"
```

---

## Step 2 — `edit` / `note_created` の収集開始 (所要2〜3時間)

> **CLAUDE.md「1 session, 1 feature」に従い、Step 1 とは別セッション・別コミットにすること。**

### 設計判断: どうやって「人手の編集」を検出するか

Obsidian には編集フックが無いので、**mtime 走査**しかない。
形は `snapshot_open_files()` (`lib/engagement_log.py:243-283`) と完全に同じ:
状態ファイルに前回値を持ち、差分だけをイベント化し、15分毎にスケジューラが叩く。
**ローカルファイル読み取りのみ・API課金ゼロ。**

**ここが本質的な問題**: mtime が動いた理由は2つある。

```
mtime が変わった
├── 人間が Obsidian で編集した        → edit (+3) ← 欲しいのはこれだけ
└── エージェント自身が書き込んだ      → ノイズ
    (tool_create_note / tool_update_note / tool_refactor_note /
     ニュース・クリプト・キュレーションの定時ジョブ)
```

素朴に mtime 差分を全部 `edit` にすると、**定時ジョブが毎日ノートを書くたびに
「人手の編集 +3」が入る**。Phase 2 のスコアラはエージェント自身の書き込みを
「ユーザーの強い嗜好」として学習することになり、静かに壊れる。

#### 採用する解法

**エージェントの書き込み側に `note_created` のライブ記録を足し、
走査側はそれを見て打ち消す。** これで未配線だった `note_created` も同時に埋まる。

```
tool_create_note / tool_update_note / tool_refactor_note
    └→ 書き込み成功時に log_event("note_created", <vault相対パス>, "agent")

15分毎の走査 (新規 scan_note_edits)
    mtime が前回より新しい
    └→ 直近の note_created に同じパスがあるか?
        ├── ある → エージェントの書き込み。baseline だけ更新し、edit は記録しない
        └── ない → 人手の編集。edit を記録
```

**なぜログを真実の情報源にするか**: 状態ファイルを両側から書くと、書き込み順序に
依存する競合が入る (bot と scheduler は別プロセス)。`read_events(days=1)` は
安価で、既に `existing_src_ids()` で同じ読み方をしている実績がある。

#### 失敗方向を先に決める (CLAUDE.md「判定器の失敗方向を設計で決める」)

> 同一パスを同じ走査間隔内で「エージェントが書き、人間も編集した」場合、
> 打ち消しが勝って `edit` を取りこぼす (偽陰性)。
>
> **これは意図的にこの方向に倒す。** 逆 (偽陽性) にすると定時ジョブの書き込みが
> 人手の編集として毎日混入し、CTR より件数の多いノイズ源になる。
> **取りこぼしは薄まるだけだが、混入は学習を反転させる。**

この段落は実装コードのコメントにもそのまま残すこと。

### 走査対象

`scripts/backfill_engagement.py:44` の `NOTE_TARGETS` を**再利用する** (新定義を作らない):

```python
NOTE_TARGETS = [
    ("30_Main/Knowledge_Base", True),   # 再帰
    ("30_Main", False),                 # 直下のみ
]
```

このリストには既に「嗜好を含まない機械生成ノートは対象外 (人手起点だけを見る)」
という判断が込められている。`02_News` / `03_Crypto` / `40_Report` / `XTrend` /
`50_Archive` は全部エージェントの書き込み先なので入っていない。
**二重定義すると片方だけ更新される** ので、`lib/engagement_log.py` 側へ移して
backfill から import する形にするか、backfill から import して使う。

### 実装順序 (50行チェックポイント)

1. `lib/engagement_log.py` に `diff_note_mtimes(current, previous)` を**純関数**で追加
   → ここで一旦止めてテストを書く (`diff_open_files` と同じく DI でテスト可能な形に)
2. `scan_note_edits(vault_root=None, ...)` を追加。`seed_only=True` を必ず用意する
   → **初回は baseline が無いので全ファイルが「編集された」になる。**
     `snapshot_open_files` の `seed_only` と同じ理由・同じ形で回避する
3. 追記失敗時は**状態を据え置く** (`snapshot_open_files` L277-280 と同じ。
   状態を進めると差分が永久に再検出されない)
4. `os.replace` による原子的な状態保存 (`_save_open_state` と同じ)
5. `tools/tool_obsidian.py` の3関数に `note_created` フックを追加
   → `tool_create_note` (L192) / `tool_update_note` (L273) / `tool_refactor_note` (L492)
   → **`is_failure_result()` で成功を確認してから記録する** (ツール層は例外を投げず
     `return f"Error: {e}"` を返す規約。「例外が出なかった＝成功」は禁止)
6. `agent_scheduler.py` にジョブ登録 (L365 の Engagement Snapshot の直後)

```python
{
    "every_minutes": 15,
    "name": "Engagement Scan (edited notes)",
    "func": scan_note_edits,
    "silent": True,
},
```

### 遵守事項

- `# -*- coding: utf-8 -*-` と `open(..., encoding='utf-8')` を明示
- 新規テストの冒頭に **`sys.stdout.reconfigure(encoding='utf-8')`** を必ず入れる
  (`tests/test_verifier.py` はこれが無くて cp932 コンソールで全通過なのに赤くなっていた)
- 絶対 import (`from lib.engagement_log import ...`)
- 例外は全て握りつぶす (ログ機構が本体を落とさない — Phase 1 の設計方針を維持)
- パス表記は `os.path.relpath(...).replace("\\", "/")` で **Vault相対・スラッシュ区切り**に
  揃える (`open` / `note_created` と突合するため。`collect_note_events` L154 と同じ)

### 検証

`tests/test_engagement_log.py` に追記 (現在65 checks):

- [ ] `diff_note_mtimes`: 新規ファイル / mtime 増加 / 変化なし / 削除された
- [ ] `scan_note_edits(seed_only=True)` がイベントを1件も書かない
- [ ] 追記失敗時に状態ファイルが進まない (次回再試行される)
- [ ] `note_created` が直近にあるパスは `edit` にならない (打ち消しの回帰)
- [ ] `note_created` が無いパスは `edit` になる (**逆方向も固定する**。
      打ち消しが効きすぎて全部消える不具合を、片方向のテストだけでは捕まえられない)
- [ ] `tool_create_note` が `Error:` を返したとき `note_created` を記録しない

実機での確認 (これが本番):

```bash
py verify_project.py
py -c "from lib.engagement_log import scan_note_edits; print(scan_note_edits(seed_only=True))"
```

- [ ] Obsidian で `30_Main/Knowledge_Base` のノートを1つ手で編集して保存
- [ ] `py -c "from lib.engagement_log import scan_note_edits; print(scan_note_edits())"` → **1件**
- [ ] `/taste` に `edit: 1` が出る (Step 1 がここで効いてくる)
- [ ] Discord で `/agent` にノート追記をさせる → 再走査 → **`edit` が増えないこと**
      (打ち消しが実環境で効いているかの確認。ユニットテストだけでは不十分)
- [ ] `scripts\arms.bat restart` 後、15分待って `/taste` で `edit` が自然増していないこと
      (定時ジョブの書き込みが混入していないか)

### コミット

```bash
git add lib/engagement_log.py tools/tool_obsidian.py agent_scheduler.py tests/test_engagement_log.py scripts/backfill_engagement.py
git commit -m "feat(personalization): edit/note_created のライブ収集を追加"
```

---

## Step 3 — スコアラの出口条件を決める (所要15分・コード変更なし)

Step 0 の数字を見て、**「いつ Phase 2 に着手するか」を件数で書き下す**。
「そのうち」「もう少し貯まったら」は判断ではないので残さない。

推奨する条件 (Step 0 の実測で調整すること):

```
着手条件: impression >= 150 かつ click >= 30 かつ 異なる curate セッション >= 20回
```

根拠: `/curate` は1回3件提示なので impression 150 ≒ 50セッション。
CTR を branch (トピック分類) 別に割ると1分類あたり10〜20件になり、
ようやく「この分類は選ばれない」が偶然と区別できる水準。

**この条件を `CLAUDE.md` か本ファイルに追記して、`/taste` を見るたびに
達成度を確認できるようにする。** 到達していなければ触らない、が正しい運用。

到達を早めたいなら、待つのではなく `/curate` を意識的に使う (1日1回で
約2週間、1日3回なら5日で到達する)。**これは「時間を空ける」ではなく
「使用回数を貯める」なので、放置では進まない。**

---

## Verification (全体)

- [ ] Step 0 の数字をメモした
- [ ] `py verify_project.py` → ALL SYSTEMS GO (各 Step の commit 前に毎回)
- [ ] `py -m pytest tests/test_engagement_log.py -q` 相当が全通過
      (このプロジェクトの慣習に合わせて `py tests/test_engagement_log.py` でもよい)
- [ ] `/taste` が CLI 出力と一致する
- [ ] 手編集で `edit` が1件増え、エージェントの書き込みでは増えない
- [ ] スケジューラ再起動後15分で `edit` が自然増しない
- [ ] Step 3 の着手条件が文書に残っている

## Rollback

Step 1/2 は独立したコミットなので個別に戻せる。

```bash
git revert <commit>          # 実装を戻す
```

収集したデータを捨てたい場合のみ:

```bash
# logs/engagement.jsonl から edit イベントだけ除去 (append-only なので通常は不要)
py -c "import json;[print(json.dumps(e,ensure_ascii=False)) for e in __import__('lib.engagement_log',fromlist=['x']).read_events() if e.get('event')!='edit']" > logs/engagement_clean.jsonl
```

**`logs/engagement.jsonl` 自体は消さないこと。** query 293件を含む
バックフィル済みの過去分が入っており、`src_id` で冪等とはいえ Chatlog の
カットオフ判定 (`_live_query_cutoff`) が壊れると再投入で二重計上する。

状態ファイル (`logs/engagement_open_state.json` および Step 2 で追加する
mtime 状態ファイル) は削除しても `seed_only=True` で作り直せる。

---

## 見積もり

| Step | 所要 | 変更ファイル | データ待ち |
|---|---|---|---|
| 0 | 2分 | なし | — |
| 1 | 30分 | 2 | 不要 |
| 2 | 2〜3時間 | 5 | 不要 |
| 3 | 15分 | 1 (文書) | — |

Step 1 だけでも単体で価値がある (溜まっている354件+αが初めて見える)。
時間が無ければ Step 0 → Step 1 → コミットで一旦切ってよい。
