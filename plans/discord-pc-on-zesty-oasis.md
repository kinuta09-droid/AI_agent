# 施策① BitNet 二段ルーティング — 計画・完了記録

ブランチ: `claude/gemini-new-models-integration-lqnjji`

## Goal
CLI ルーター `main.route_to_tool()` の一次判定にローカル BitNet を前置し、
**引数不要ツールに高確信で一致した時だけ** Gemini 日次枠をスキップする二段構成にする。
曖昧・引数必要・常駐起動・exe 不在は従来 Gemini パスへ縮退させる（利得はコスト削減と高速化、
リスクは低品質ローカルモデルによる誤ルーティング）。

## Steps → Result
1. `tools/tool_bitnet.py` に `route_tool_confident()` 追加
   → 高確信(forward一致)のみツール名、曖昧/catchall/空/例外は `(None, reason)` を返す確信度シグナル版。
2. `main.route_to_tool()` に BitNet 一次判定を前置
   → no-arg かつ **long_running でない** ツールに高確信一致した時のみ Gemini スキップ。
3. `tests/test_bitnet_router_gate.py` 追加（`_run_bitnet` / `route_tool_confident` をモック）
   → exe・モデル・Gemini 非依存で Linux/CI でも動く 16 ケース。
4. Windows 実機確認 → `verify_project.py` = ALL SYSTEMS GO / `main.py --menu` = 全15ツール描画。
5. `/grill` レビュー → Critical 1件を実証・修正。

## /grill で発見・修正した Critical
- **long_running ツールの無確認オートローンチ**（実証済み）。
  修正前ゲートは `needs_arg` のみ判定 → `discord_bot` / `scheduler`（needs_arg=False, long_running=True）に
  BitNet が高確信一致すると Gemini をスキップし、menu から常駐プロセスを無確認起動しうる。
  ライブ再現: 「スケジューラーを起動」→ `⚡ BitNet → [scheduler] (Gemini skip)`。
  **修正**: スキップ条件に `and not chosen.get("long_running")` を追加。
  再確認: 同入力が `{'tool':'scheduler','arg':'start'}`（Gemini 経由 = arg 非 None）になった。

## Verification（すべて実施済み）
- [x] `python -m unittest tests.test_bitnet_router_gate` = 16 passed
- [x] `verify_project.py` = ALL SYSTEMS GO（修正後も clean）
- [x] `main.py --menu` = メニュー全描画・EOF で clean exit
- [x] ライブ probe: no-arg clean match は BitNet skip、long_running は Gemini 経由

## 既知トレードオフ / 残課題
- レイテンシ: BitNet 一次判定は毎回 ~1–2s 走る。needs_arg / 曖昧は「BitNet + Gemini」で
  従来より遅い。利得が出るのは no-arg clean match のみ（意図した設計）。
- forward 一致の部分文字列衝突は現 TOOLS では発生しないが、将来ツール名に包含関係が
  できると誤マッチしうる（潜在 warning。完全一致 or 語境界化で対処可能）。

## Rollback
`main.route_to_tool` の一次判定ブロックを削除すれば従来 Gemini 単段に戻る（純加算のため安全）。
