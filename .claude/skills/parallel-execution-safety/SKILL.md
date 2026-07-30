---
name: parallel-execution-safety
description: Plan実行のfan-out/並列化 (engine_plan_execute.py, lib/concurrency.py, tool_registry の parallel_safe 宣言) を触るときの既知バグパターン。ツールを並列実行してよいかどうかの判定基準、depends_on/args_json の扱い、副作用のあるフォールバック経路の分類に関する規約。
---

# Parallel Tool Execution Safety

- **並列化の効果は「最も遅い1本」に律速される。速い方を何本足しても縮まらない**。Phase C1 の fan-out は、同種3件 (`tool_web_search` ×3) では 12.3秒 → 4.2秒 (2.9x) だったが、`web_search` (4.5秒) + `vault_hybrid_search` (16.3秒) の2件では 20.8秒 → 16.3秒 (1.3x) にしかならない — 速い方が遅い方の中に完全に隠れるだけだから。**遅い1本を速くする方が効く局面がある**ことを、並列化に手を出す前に見積もること。なお `saved_seconds` は各ステップの所要時間に**共有資源の待ち時間** (`lib.concurrency.ollama_slot`) が混ざるので、資源を奪い合うステップを並べると過大に出る → 最低保証値として読まない (2026-07-28・PR #29)
- **並列化の依存判定を LLM に書かせない。既に持っている契約を読む**。Planner に `depends_on` を宣言させると、依存を1本落とした競合状態が黙って混入する (落とし方が安全側にならない)。`generate_plan` のプロンプトは既に「前ステップの結果に依存する引数は `args_json` を空にして `args_hint` だけ書く」と指示しているので、**`args_json` が使える＝データ依存が無い**と読めば新しい判定は要らない。これに「レジストリで `parallel_safe` が宣言されている」を AND して副作用の順序依存も潰す。**並列可否は必ず opt-in** — 付け忘れは逐次のまま (安全) だが、誤って付けるとデータ競合が黙って起きる、という非対称性があるため。`tests/test_tool_registry.py` が集合をピン留めし、**どちらのリストにも載っていないツールがあると落ちる** (「判断されていない」状態を作らせない)
- **「取得系だから並列にしてよい」を呼び出し口の名前で決めない**。`tool_get_calendar` は取得系だが、主経路 (gws CLI) が失敗したときのフォールバック `_get_calendar_service()` が (a) `token.json` を書き換え、(b) 期限切れ時に `InstalledAppFlow.run_local_server()` でブラウザ認証を待つ。ワーカースレッドでこれが起きるとウェーブ全体が無言で止まる。**実装を最後まで読んでから分類する**
