# 自己拡張スキル (Self-Expanding Skills) — 実装プラン

ブランチ: `claude/self-expanding-skills-rtdjeh`
起案: 2026-07-30 / 状態: **未着手（調査のみ完了）**

> このファイルは「実機（Windows）で改めて着手するための引き継ぎ書」。
> 調査済みの事実・行番号・落とし穴を全部書いてあるので、
> 新しいセッションはこのファイルだけ読めば再調査なしで Phase 1 から始められる。

---

## Goal

新機能を毎回人間が手で実装するのをやめ、エージェントが
**「一度うまくいったやり方」を手順書として保存し、次回それを検索して再利用する**
ようにする（Claude Code の `skill_creator` / `find_skill` 相当）。

---

## 大前提: 自己拡張を2種類に分ける

Claude Code の skill は **Markdown の手順書**であって実行コードではない。
だから自走生成しても安全。一方この repo の「ツール」は Discord Bot と
**同一プロセスで動く Python 関数**で、OAuth トークン・Vault 書き込み・
`trade_log.json` に触れる。同じ扱いにはできない。

```
A. 手順スキル（既存ツールの組み合わせ・自然言語）
   新しいコードは1行も走らない = 自律生成してよい   ← ★このプランの本体
B. 新ツール（Python コード生成）
   LLM が書いたコードを Bot プロセスで実行する = 承認ゲート必須（Phase 5 / 別プラン）
```

**A だけで、今まで手動実装していた「機能」のかなりの割合が吸収できる。**
「週次マージしてからレポートを作る」「銘柄分析したら過去予測と突き合わせる」は
新しいツールではなく**手順**であり、A で足りる。

---

## 調査済み: 素材は8割揃っている

| 必要な部品 | 現状 | 場所 |
|---|---|---|
| 能力の単一情報源 | ✅ ある（ハードコード） | `lib/tool_registry.py:105` `TOOL_ENTRIES` |
| 需要シグナル（何に失敗しているか） | ✅ ある | `tools/tool_skill_observer.py:84` `get_failure_summary` / `logs/skill_observations.jsonl` |
| 成功パターンの蓄積 | 🔺 ある（**手順は捨てている**） | `engine_plan_execute.py:1251` `log_execution` |
| 検索基盤（find_skill 相当） | ✅ 強力なのがある | `tools/tool_vault_hybrid.py`（BM25+bge-m3+リランカ） |
| ツール説明文の索引化 | ✅ 実績あり | `lib/activity_corpus.py:337` `render_skill_rollups` |
| 生成物の承認ゲート | ✅ 2箇所で実績あり | `engine_self_improve.py:77`（`approved: false`）/ `lib/tool_registry.py:72`（実装指示書を生成して人間に渡す） |
| **スキル（手順書）という単位** | ❌ 無い | — |

観測・学習・検索・承認は全部ある。無いのは「学んだ手順を保存して次回引ける形」だけ。
現状は毎回 Planner がゼロから計画を書き直している。

---

## アーキテクチャ

```
[観測]  logs/skill_observations.jsonl + learning_db.json
           |  同型クエリが N 回成功 / 同じ失敗が N 回
           v
[生成]  tool_skill_creator: 成功した plan を手順書 Markdown 化
           |  Vault の 90_Skills/ に保存（既存 Obsidian 基盤に相乗り）
           v
[検索]  find_skill: tool_vault_hybrid で関連手順書を 1〜2 件取得
           v
[注入]  generate_plan のプロンプトへ skill_block として差し込む
```

注入先は既に用意されている。`generate_plan` は `learning_block`(:168) /
`memory_block`(:172) / `session_block`(:175) と**同じ形の追記スロット**を持つので
（`engine_plan_execute.py:167-182`）、`skill_block` を1本足すだけで済む。

---

## 着手前に潰すブロッカー（コードを読んで確認済みの実在の障害）

1. **Planner はツール名を enum で縛っている**
   `_build_step_schema`（`engine_plan_execute.py:100`）が `"enum": list(valid_tools)`。
   レジストリに無い名前は**構造化出力の時点で物理的に出力不能**。
   これは良い性質なので壊さない → A のスキルは「ツール」ではなく
   **プロンプトへのヒント**として入れるのが正解（enum を動的にするのは B の話）。

2. **`log_execution` が plan 本体を保存していない** ← Phase 1 はこれ
   `plan_steps`（件数）と `tools_used` しか残らない（`engine_plan_execute.py:1286` 付近の entry dict）。
   **成功した手順をスキルに昇格させたくても、手順が残っていない。**

3. **`get_tools_config()` はプロセス内キャッシュ**
   `_CACHE`（`lib/tool_registry.py:251`）。B で追加したツールは再起動まで見えない。
   A は毎回ファイルを読むので再起動不要（この差も A を先にやる理由）。

4. **`_build_capability_block()` の網羅範囲**（`engine_plan_execute.py:1077`）
   能力一覧に載らない機能は「無い」と断言される（PR #19 の教訓）。
   スキルを足したらこのブロックにも入れる。さもないと
   **自分で作ったスキルの存在を否定する**新種の嘘が出る。

5. **昇格の判定に `lib.bot_errors.is_failure_result()` / `is_partial_result()` を使う**
   「例外が出なかった＝成功」で昇格させると、ツールが規約どおり `Error: ...` を
   返した実行が手順書になる（`execute_plan` で一度やらかした穴と同型）。

6. **`tests/test_tool_registry.py` の集合ピン留めは仕様**
   B で自動追加すると必ずテストが落ちる = 人間のレビューが強制される、
   という設計。**この壁は残したまま**にする。

---

## Steps → Expected Result

### Phase 1: 手順を残す（回帰ゼロ・純加算）
1. `log_execution` の entry に `plan` 本体（step/task/tool、args は入れない）を追加
   → 成功実行から手順を復元できるようになる。
   - args を入れない理由: 引数には Vault パス・検索クエリ・本文が混ざり、
     learning_db が肥大化する上に個人情報が増える。手順の再利用には不要。
   - `_rotate_executions` / `_append_archive` の容量前提が変わるので、
     1エントリのサイズ増を `tests/test_learning_db_rotation.py` で確認する。
2. 既存エントリ（`plan` を持たない過去分）を読む側は `.get("plan", [])` で参照
   → 過去分は無影響（`parallel` フィールドを足したときと同じ純加算パターン）。

### Phase 2: スキルを作る
3. `tools/tool_skill_creator.py` を新規作成
   - 入力: `exec_id`（learning_db の1実行）または手動指定のクエリ+手順
   - 出力: `90_Skills/Skill_<slug>.md`（front matter に `approved: false`・`source_exec_id`・`created` ）
   - 本文: 「いつ使うか（トリガー）」「手順（ツール名付き）」「非対象」の3節
     - **「非対象」を必ず書く**: 説明文だけが選択根拠になるのは
       ツールと同じで、書かないと縄張りを奪い合う（eval T09 の再来）
4. `/skill-create` スラッシュコマンド（人間が引き金を引く形から始める）
   → 自動昇格は Phase 4。まず人間が「これは残す価値がある」と判断する運用で癖を見る。

### Phase 3: スキルを引いて使う
5. `find_skill(query)` — `tool_vault_hybrid` に `90_Skills/` を絞って検索させ上位1〜2件
   - **注意**: Vault のノートは第三者テキストを含むので、
     `<skills>` で囲い本文中の閉じタグをエスケープする
     （`tools/tool_vault_hybrid._format_rerank_candidates` と同じ扱い）
6. `generate_plan` に `skill_block` を注入（`learning_block` と同型）
7. `_build_capability_block()` にスキル一覧を追加（ブロッカー4）

### Phase 4: 自動昇格
8. 同型クエリが N 回 `outcome == "success"` → 自動でスキル化。ただし `approved: false` のまま。
   承認済みだけが `find_skill` にヒットする（`get_approved_patch_hints` と同じゲート）。

### Phase 5: B（コード生成）— **別プラン・このプランでは着手しない**
自走させず既存2パターンの延長で: 仕様+実装コード+テストを**ファイルに落とすまで**が
エージェントの仕事。`verify_project.py` を通し、人間が PR をマージする。
Bot プロセス内で `exec()` する案は、守っている資産（OAuth・Vault・trade_log）に対して割に合わない。

---

## Verification

Phase ごとに、実機（Windows）で以下を通してからコミットする。

```bash
py verify_project.py                          # ALL SYSTEMS GO（毎 Phase 必須）
py -m unittest tests.test_learning_db_rotation  # Phase 1: エントリ肥大の回帰
py -m unittest tests.test_tool_registry         # Phase 2-3: ピン留めが生きているか
py scripts\eval_agent.py                        # Phase 3: before/after
```

- [ ] **Phase 1**: 実行1回 → `learning_db.json` の最新エントリに `plan` が入る。
      過去エントリを読む `extract_patterns` / `get_enhanced_prompt` が例外を出さない。
- [ ] **Phase 2**: `/skill-create <exec_id>` → `90_Skills/` に Markdown が生成され、
      front matter に `approved: false` が入る。
- [ ] **Phase 3**: **`scripts/eval_agent.py` を注入 OFF/ON の両方で走らせて比較**。
      「LLM を噛ませたこと自体は改善を意味しない」ので、
      **注入なしのベースラインと必ず比べる**（リランカで qwen3:1.7b が
      リランク無しを下回った件と同じ検証を課す）。
      - ON アームで Gemini 日次上限に当たると一部だけ施策が効いた計測になる。
        `scripts/eval_retrieval.is_clean_arm()` と同じ「汚染フラグを数値と一緒に持ち回る」
        扱いをこちらにも入れること。
- [ ] **Phase 4**: 昇格判定が `is_failure_result` を通っている（ブロッカー5）。
      `Error: ...` を返した実行がスキル化されないことをテストで固定。

---

## Rollback

- Phase 1: entry への `plan` 追加を消すだけ（読む側は `.get` なので純加算）。
- Phase 2: `90_Skills/` を消し、`tools/tool_skill_creator.py` を削除。他に依存が無い。
- Phase 3: `generate_plan` の `skill_block` 注入行を削除すれば従来の計画生成に戻る。
- いずれも既存経路への変更が「追記1行」に収まるよう設計しているので、
  Phase 単位で独立してロールバックできる。

---

## 進め方の注意

- CLAUDE.md「1 session, 1 feature」に従い、**Phase 1 だけをコミットして一度切る**。
  Phase 2 以降は別セッション。
- Phase 3 は3ファイル以上に触るので **Plan Mode 必須**。
- 実装後は `/grill` を通す。
