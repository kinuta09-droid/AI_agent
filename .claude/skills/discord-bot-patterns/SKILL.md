---
name: discord-bot-patterns
description: discord.py の View/Button 実装、Bot再起動をまたぐ永続化、interaction token の有効期限、失敗プレフィックス判定まわりの既知バグパターン。commands/bot_views.py や discord.ui.View を継承するクラス、bot_commands_*.py の結果表示を触るときに読む。
---

# discord.py Bot: Views, Timeouts & Result Display

- **`discord.ui.View` の `timeout=None` は「Bot 再起動をまたいで生きる」宣言であって、単なる無期限ではない**。`bot.add_view()` での再登録とセットでなければ、再起動後はボタンが押しても無反応 (Discord 側に「インタラクションに失敗しました」が出るだけで、ログには一行も残らない = 無言の死)。おまけに discord.py の View ストアから永久に消えずメモリを食う。永続化の条件は **固定 custom_id + コールバックがステートレス** (毎回ファイルを読み直す) の2つで、instance 変数に作業状態を持つ View は原理的に永続化できない -> その場合は**有限 timeout + `on_timeout` でボタンを無効化**する (2026-07-26 に `ClientReviewLoopView` / `OpenInObsidianView` を是正)。`tests/test_button_views.py` の静的スキャンが「timeout=None なのに add_view されていない View」を落とす
- `on_timeout` から `interaction.followup.send()` の戻り (WebhookMessage) を編集して期限切れを通知する場合、**View の timeout は interaction token の寿命 (発行から15分) より短くする**。15分ちょうどにすると通知の edit がトークン失効と同時になり、通知だけが無言で失敗する。なお `discord.ui.View` に `message` 属性は自動では入らない (自分で代入する)。`view.timeout = 秒数` は実行中のタイマーの期限も更新できるので、長時間処理の前後で延長/復帰させる
- 例外メッセージを整形し直すときは **`startswith("Error:")` で分岐している箇所を先に洗う**。`commands/bot_commands_agent._FAILURE_PREFIXES` はコロン固定だったため、`Error (RuntimeError): ...` 形式にすると失敗結果を成功扱いして次回 `/agent` のセッション文脈に混ぜるところだった
