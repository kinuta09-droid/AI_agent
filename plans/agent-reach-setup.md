# Agent Reach 実機導入計画

## 役割分担 — どのセッションが何をやるか

| セッション | 環境 | やること |
|---|---|---|
| **リモートセッション (完了済み)** | Linux サンドボックス | `docs/agent-reach.md` と本計画書を書いて push。**実機インストールは物理的に不可能** |
| **実機セッション** | Windows / `C:\AI_agent` | **STEP 1〜5 のすべて。** インストール・doctor 通し・判断・記録 |

### 実機セッションの始め方
```
cd C:\AI_agent
git fetch origin claude/agent-reach-mcp-k91vmv
git checkout claude/agent-reach-mcp-k91vmv
```
その上で Claude Code を起動し、最初にこう指示する:
> `plans/agent-reach-setup.md` を読んで、STEP 1 から始めて

---

## Goal

Agent Reach を開発機に入れ、**X/Twitter・YouTube・Reddit の一次情報をエージェントが
直接読める状態**にする。`.mcp.json` は変更しない (Agent Reach は MCP ではない — 根拠は
`docs/agent-reach.md`)。

**このリポジトリのコードは1行も変えない。** 変更が入るのは開発機のユーザー環境
(`~/.claude/skills/`、pipx の venv、yt-dlp の config) だけ。`tool_xtrend.py` の
置き換えは**この計画の範囲外**（1 session, 1 feature）。

---

## Context — リモートで検証済みの事実 (v1.5.0)

STEP を書くにあたって実際に踏んだ地雷。**同じところで止まらないために先に読む。**

| 事象 | 実測 |
|---|---|
| `pip install agent-reach` | **失敗**。PyPI に存在しない → `git+https://` を使う |
| `pip install agent-reach[mcp]` (コード内の案内文) | **その extra は無い**。定義は `browser`/`cookies`/`all`/`dev` のみ |
| pipx で入れると `yt-dlp` が **PATH に出ない** | 依存として venv 内には入るが shim が作られず、doctor が「yt-dlp 未安装」と誤報する → STEP 2 の `pipx inject --include-apps` が必要 |
| `pipx install --include-deps` | yt-dlp は出るが `dotenv` `idna` `pygmentize` など**余計な shim が13個**生える。使わない |
| pipx が `uv` バックエンドを選んで失敗 | `pipx needs uv>=0.9.17, but ... reports 0.8.17` → `--backend pip` を付ける |
| `agent-reach doctor` の出力 | **中国語**。`[X]` = 未インストール / `[!]` = 要設定 / `✅` = 使える |
| `skill --install` の書き込み先 | `~/.claude/skills/agent-reach/` (**ユーザースコープ**。リポジトリには入らない) |

---

## STEP 1 — pipx を用意する

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

**期待結果**: `ensurepath` が `%USERPROFILE%\.local\bin` を PATH に追加したと出る。

> ⚠️ **PATH 反映のためターミナルを開き直す**（Claude Code も再起動）。ここを飛ばすと
> STEP 2 以降で `agent-reach` が「コマンドが見つかりません」になる。

**確認**: 新しいターミナルで `pipx --version` がバージョンを返す。

---

## STEP 2 — Agent Reach 本体 + yt-dlp shim

```powershell
pipx install git+https://github.com/Panniantong/agent-reach.git
pipx inject --include-apps --force agent-reach yt-dlp
```

- `--include-apps` が無いと **yt-dlp が PATH に出ず、YouTube チャネルが永久に `[X]`** になる
  (Context の表を参照。依存としては入っているのに doctor は「未安装」と言う)
- `--force` が要るのは、yt-dlp が既に venv 内にあると pipx が
  `⚠️ yt-dlp already seems to be installed` で**何もせず終わる**ため
- pipx が uv エラーを吐いたら **両方のコマンドに `--backend pip` を足す**

**期待結果**:
```
$ agent-reach version
Agent Reach v1.5.0
$ yt-dlp --version
2026.xx.xx
```

**要らなくなったら**: `pipx uninstall agent-reach` で venv ごと消える。

---

## STEP 3 — Skill を登録する

```powershell
agent-reach skill --install
```

**期待結果**: `Skill installed for Claude Code: C:\Users\<user>\.claude\skills\agent-reach`

`SKILL.md` + `references\{search,social,career,dev,web,video}.md` の7ファイルが置かれる。

> **ユーザースコープなので、このリポジトリには入らない。**
> 別のチェックアウト・別マシン・Claude Code on the web のセッションでは毎回やり直しが要る。
> （リモートセッションでの検証時は、この直後から `agent-reach` skill がロードされた）

**確認**: Claude Code を再起動し、`/` で `agent-reach` skill が候補に出る。

---

## STEP 4 — doctor を通して現状を把握する

```powershell
agent-reach doctor
```

**素の状態で `✅` になるはずのもの**: 任意のウェブページ (Jina Reader) / RSS・Atom

**`[!]` や `[X]` が出る主なもの と、直すかどうかの判断**:

| チャネル | doctor の言い分 | 判断 |
|---|---|---|
| YouTube | `yt-dlp 已安装但未配置 JS runtime` | **直す** → STEP 4-a |
| GitHub | `gh CLI 未安装` | **直す** → `winget install --id GitHub.cli` (このプロジェクトは GitHub 作業が多い) |
| 全網語義搜索 (Exa) | `需要 mcporter + Exa MCP` | **直さない** → 判断ポイント①を読む |
| Twitter/X | 要 cookie | **直す** → STEP 5 (この導入の主目的) |
| Reddit / 小紅書 / Facebook / Instagram 等 | 要ログイン | **今回は直さない**。必要になった時に個別に |

### STEP 4-a — yt-dlp の JS runtime 設定

`doctor` は**OS を見て PowerShell 用のコマンドを出力する**ので、**画面に出たブロックを
そのまま貼る**（パスをこちらで決め打ちしない）。中身はこういう形:

```powershell
$cfg = '<doctor が出したパス>'
New-Item -ItemType Directory -Force -Path (Split-Path $cfg) | Out-Null
if (-not (Test-Path $cfg) -or -not (Select-String -Path $cfg -Pattern '--js-runtimes' -Quiet)) {
  Add-Content -Path $cfg -Value '--js-runtimes node'
}
```

> **スコープ確認** — これは yt-dlp のユーザー設定ファイル (`%APPDATA%\yt-dlp\config` 相当) を
> 1行追記するだけで、**PowerShell プロファイルにも環境変数にもレジストリにも触らない**
> (CLAUDE.md の Encoding Safety Rules で言う LEVEL 3/4 には該当しない)。
> Node.js は `chrome-devtools` MCP のために既に入っているはず。無ければ先に入れる。

**確認**: `agent-reach doctor` で YouTube が `✅` になる。

---

## STEP 5 — Twitter/X の cookie を通す（この導入の主目的）

`tool_xtrend.py` のセレクタ崩れの代替経路を確保するのが狙い。

**推奨（ブラウザから自動抽出）**:
```powershell
agent-reach configure --from-browser chrome --platform twitter
```

**手動（Cookie-Editor 拡張で `auth_token` と `ct0` を取る場合）**:
```powershell
agent-reach configure twitter-cookies "auth_token=xxxx; ct0=yyyy"
```

> 🔐 **cookie は `.env` にもリポジトリにも置かない。** `agent-reach configure` が
> `~/.agent-reach/` に保存する。CLAUDE.md の Secrets ルールどおり、コミット対象外の場所に
> 留めること。ターミナル履歴に残るのが嫌なら `--from-browser` の方を使う。

**確認**: `agent-reach doctor` で Twitter/X が `✅`。実際に1本読ませて中身が返るか見る。

---

## 判断ポイント

### ① Exa 検索 (mcporter) を入れるか → **既定は「入れない」**
`npm install -g mcporter` + `mcporter config add exa ...` という**グローバルな system 変更**が要る。
一方このプロジェクトには既に **Tavily** (`tool_curator.py`) がある。**Web 検索は増やさず、
Agent Reach は「Tavily で拾えない一次情報 (X / YouTube / Reddit)」に絞る**のが導入の趣旨。
Tavily の結果に不満が出てから改めて検討する。

### ② `requirements.txt` に足すか → **足さない**
Agent Reach は Bot のランタイム依存ではなく**開発者/エージェント用 CLI**。足すと Bot の
デプロイが毎回 GitHub tarball を引きに行くうえ、MCP サーバーを使う場合の `mcp<2` ピンまで
巻き込む。pipx で環境を分離したままにする。

### ③ `agent-reach install --env=auto` を走らせるか → **走らせない**
このインストーラは gh CLI や mcporter を**勝手に入れにいく**。リモートでの `--dry-run` では
`Would install mcporter and configure Exa search` まで出た（判断ポイント①に反する）。
STEP 2〜5 で必要なものだけ手で入れる方針。動きを見たいだけなら
`agent-reach install --env=local --dry-run` で**変更なしのプレビュー**にとどめる。

---

## Verification（完了条件）

- [ ] `agent-reach version` → `Agent Reach v1.5.0` 以上
- [ ] `yt-dlp --version` が PATH 経由で返る（STEP 2 の inject が効いている証拠）
- [ ] `agent-reach doctor` で **Web / RSS / YouTube / GitHub / Twitter が `✅`**
- [ ] Claude Code 再起動後、`agent-reach` skill がロードされている
- [ ] 実タスクで1本通す: 「X で〇〇について何が話されているか調べて」→ 実データが返る
- [ ] `git status` が **clean**（この計画はリポジトリを変更しない。汚れていたら何かがおかしい）
- [ ] `py verify_project.py`（フル）が ALL SYSTEMS GO

## Rollback

```powershell
agent-reach uninstall          # 設定・トークン・SKILL.md を削除
pipx uninstall agent-reach     # venv ごと削除 (yt-dlp shim も消える)
```
yt-dlp の config に足した `--js-runtimes node` の1行だけは手で消す（他ツールに影響しないので
残しても実害はない）。**リポジトリ側にロールバック対象は無い。**

## 記録

STEP 4 の doctor 出力で「実際にどのチャネルが使えるようになったか」が確定するので、
`docs/agent-reach.md` の「このプロジェクトでの使いどころ」を実測に合わせて更新し、
つまずいた点があれば Context の表に追記してコミットする。
