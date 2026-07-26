# PC 起動時の自動起動と、立ち上がらないときの調べ方

## 日常操作 (ターミナル不要)

| やりたいこと | 手段 |
|--------------|------|
| コード変更を Bot に反映 | Discord `/restart target:bot` |
| Scheduler だけ入れ直す | Discord `/restart target:scheduler` |
| 今の稼働状況を見る | Discord `/status` または `arms.bat status` |
| 落ちている / PC 再起動後に上げる | デスクトップの **ARMS Control** をダブルクリック → `1: start` |
| スタックを止める | `arms.bat stop` (メニューなら `4`) |
| ランチャーのログを見る | `arms.bat logs` (メニューなら `5`) |

再起動の実体は「子プロセスを終わらせるだけ」で、再生成は `main.py` の
スーパーバイザが行う。新しい python プロセスとして起動されるので、
**再起動すればコード変更は反映される**。

`main.py` 自身を書き換えたときだけは Discord から再起動できない
(スーパーバイザを殺すと誰も再生成しないため)。`arms.bat restart` を使う。

ショートカットの作成 (1回だけ):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
```

## 監視の守備範囲

| 障害 | 検知するもの | 復旧 |
|------|--------------|------|
| プロセスが死んだ | `main.py` の `poll()` | 5秒後に再起動 |
| 生きているが機能停止 (ハング) | heartbeat 鮮度 (`lib/watchdog.py`, 300秒) | 実体を kill → 再起動 |
| 起動直後に落ち続ける | `is_fast_exit` + 連続失敗カウント | 指数バックオフ + Discord 通知 |
| **`main.py` 自体が起動していない** | **なし (監視者がいない)** | **タスクスケジューラ頼み** |

最後の1行が重要で、heartbeat は「`main.py` が動いていること」を前提にした仕組み。
PC 起動時に `main.py` が走らなければ heartbeat は一度も評価されず、当然通知も出ない。

## 起動経路 (なぜ3ファイルに分かれているか)

```
タスクスケジューラ "ARMS Launcher"
  └ scripts\start_arms.bat     … 入口。非表示で本体を起動して即 exit
      └ scripts\arms_hidden.vbs … WScript.Shell.Run(..., 0, False) = 非表示
          └ scripts\arms_core.bat … ログ退避 + py -u main.py (実処理)
```

**可視のコンソールウィンドウを作らないこと**が要件。タスクが `.bat` を直接実行すると
黒いウィンドウがデスクトップに残り、その × を押すと `CTRL_CLOSE_EVENT` が飛んで
`main.py`・Bot・Scheduler が一斉に死ぬ (終了コード `0xC000013A`)。
実質「スタック全滅ボタン」になるため、必ず `wscript` 経由の非表示起動にする。
2026-07-26 にこれで全停止した。

これら `.bat` / `.vbs` は **ASCII のみ**で書くこと。cmd.exe は `.bat` を cp932 で読むため、
UTF-8 の日本語コメントは化けるうえ、`。` の末尾バイトが行末の CR を食べて
次の実行行をコメントに連結してしまう (終了コード 9009 で即死する)。
日本語の説明はこの `.md` に書く。

## 自動起動の登録 (タスクスケジューラ)

管理者 PowerShell で1回だけ実行する:

```powershell
schtasks /create /tn "ARMS Launcher" /tr "C:\AI_agent\scripts\start_arms.bat" /sc onlogon /delay 0001:00 /rl highest /f
```

- `/sc onlogon` … ログオン時に起動。`onstart` (ブート時) はユーザープロファイルや
  ネットワークが揃う前に走りやすく、Discord ゲートウェイ接続に失敗しがち。
- `/delay 0001:00` … 1分待ってから起動。ネットワーク未接続での起動失敗を避ける
  (万一失敗してもバックオフ再試行で自力復帰はする)。

確認と手動実行:

```powershell
schtasks /query /tn "ARMS Launcher" /v /fo list   # 登録内容と前回結果
schtasks /run   /tn "ARMS Launcher"               # 手動で起動テスト
```

`前回の実行結果` が `0x0` 以外なら、タスク自体が失敗している。

## 立ち上がらなかったときの確認順

1. **タスクが動いたか**
   `schtasks /query /tn "ARMS Launcher" /v /fo list` の「前回の実行時刻」を見る。
   ブート時刻より後になっていなければ、原因は Bot ではなく自動起動の設定。

2. **ランチャーの記録**
   `logs\launcher.log` (今回) / `logs\launcher.prev.log` (前回のブート) を見る。
   ここが空・存在しないなら `start_arms.bat` 自体が走っていない。

3. **起動失敗ループになっていないか**
   ログに `[Monitor] ⚠️ ... Restarting in Ns (consecutive startup failures: N)` が
   並んでいれば、実体が毎回即 exit している。同じログに終了コードが出る。

4. **多重起動ガードで止まっていないか**
   `[Discord] ⚠️ Another bot instance is already running (PID xxxx)` が出ていたら、
   PID ファイル (`bot_discord.pid` / `agent_scheduler.pid`) の所有者判定で弾かれている。
   再起動をまたいだ残骸は `lib/pid_guard.py` がブート ID で自動回収するが、
   同一ブート内で本当に二重起動しているケースはこのメッセージが正しい。
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | select ProcessId,CommandLine`
   で実際のプロセスを数えて確認する。

   `arms.bat status` (= Discord `/status` と同じ表示) で PID と heartbeat 鮮度を
   まとめて確認できる。Launcher の行が `not running` なら 1. に戻る。

5. **heartbeat の鮮度**
   `logs\bot_discord.hb` / `logs\agent_scheduler.hb` の更新時刻。
   30秒毎に更新されていれば本体は健全。
   ```powershell
   Get-ChildItem logs\*.hb | select Name, LastWriteTime
   ```
