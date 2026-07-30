# Agent Reach 導入手順

> 対象: 開発機 (Windows / `C:\AI_agent`)
> 最終検証: 2026-07-30 / Agent Reach v1.5.0

## これは MCP サーバーではない

「Agent Reach MCP」という呼ばれ方をするが、**このツールの本体は MCP サーバーではなく
CLI (`agent-reach`) + Agent Skill (`SKILL.md`)** である。導入前にここを取り違えると、
`.mcp.json` に登録して「何も増えない」という結果になる。

リポジトリ内には `agent_reach/integrations/mcp_server.py` が確かに存在するが、
**公開しているツールは `get_status` の1個だけ** (`agent-reach doctor` の結果を JSON で
返すだけ) で、インターネットアクセス機能は一切含まれていない。実測:

```
$ python -m agent_reach.integrations.mcp_server   # initialize + tools/list を流し込む
{"result":{"tools":[{"name":"get_status",
  "description":"Get Agent Reach status: which channels are installed and active.",
  "inputSchema":{"type":"object","properties":{}}}]}}
```

実際の読み取り/検索は、Agent Reach が導線を整えた**上流ツール (yt-dlp / gh / Jina Reader /
twitter-cli など) をエージェントが直接叩く**設計になっている。その叩き方を教えるのが
`SKILL.md` であり、そこが導入の本体。**そのため `.mcp.json` には登録していない。**

## インストール (開発機)

> **実行手順は `plans/agent-reach-setup.md` にステップ化してある。**
> 実機で入れるときはそちらを上から順に進めること。以下は要約。

Agent Reach は **PyPI に無い**。GitHub から直接入れる。

```bash
py -m pip install --user pipx
py -m pipx ensurepath
# ここでターミナルを開き直す (PATH 反映)
pipx install git+https://github.com/Panniantong/agent-reach.git
pipx inject --include-apps --force agent-reach yt-dlp
```

`pipx` を使うのは、これが Bot のランタイム依存ではなく**開発者/エージェント用の CLI** だから。
`requirements.txt` には**入れない** (入れると Bot のデプロイが GitHub tarball を引きに行く)。

2行目の `inject` が要るのは、`yt-dlp` は依存として venv に入るのに **shim が作られず PATH に
出ない**ため。これを飛ばすと doctor が YouTube を「yt-dlp 未安装」と誤報し続ける。

続けて Skill をエージェントに登録する:

```bash
agent-reach skill --install
```

`~/.claude/skills/agent-reach/` に `SKILL.md` + `references/{search,social,career,dev,web,video}.md`
が配置される (**ユーザースコープなので、このリポジトリにはコミットされない** — 新しい
チェックアウトや Claude Code on the web のセッションでは毎回入れ直しが要る)。

導通確認:

```bash
agent-reach doctor
```

素の状態で使える channel は Web (Jina Reader) / RSS / GitHub (gh CLI) / YouTube (yt-dlp) /
V2EX / B站検索 の6つ。Twitter/X・Reddit・小紅書などは cookie かログインが要る。

## 検証済みの落とし穴

| 事象 | 実際 |
|---|---|
| `pip install agent-reach` | **失敗する** — PyPI に存在しない。`git+https://...` を使う |
| `pip install agent-reach[mcp]` (コード内の案内文) | **その extra は無い**。定義されているのは `browser` / `cookies` / `all` / `dev` のみ |
| MCP サーバーを mcp SDK 2.x で起動 | **クラッシュ** — `AttributeError: 'Server' object has no attribute 'list_tools'`。2.0.0 で低レベル `Server` からデコレータ API が消えたため。動かすなら `mcp<2` にピン留めが必要 (1.29.0 で動作確認済み) |
| pipx 導入後に `yt-dlp` が PATH に無い | 依存として venv には入るが shim が作られない。`pipx inject --include-apps --force agent-reach yt-dlp` で出す (`--force` が無いと「already installed」で何もせず終わる) |
| `pipx install --include-deps` で代用 | yt-dlp は出るが `dotenv` `idna` `pygmentize` など**余計な shim が13個**生える。使わない |
| pipx が `uv` バックエンドで失敗 | `pipx needs uv>=0.9.17, but ... reports 0.8.17` → 各コマンドに `--backend pip` を付ける |
| `agent-reach doctor` の出力 | **中国語**。`[X]`=未インストール / `[!]`=要設定 / `✅`=使える |
| `agent-reach skill --install` の書き込み先 | `~/.claude/skills/` (ユーザースコープ)。リポジトリの `.claude/skills/` ではない |
| `agent-reach install --env=auto` | gh CLI や mcporter を**勝手に入れにいく**。`--dry-run` で確認するに留め、必要なものだけ手で入れる |

## このプロジェクトでの使いどころ

- **`tools/tool_xtrend.py`** — X/Twitter のセレクタ崩れが慢性的な箇所。Agent Reach の
  twitter channel は cookie 認証 + CLI バックエンドなので、スクレイピングより壊れにくい。
  ただし置き換えは挙動が変わるので、別セッションで（1 session, 1 feature）。
- **`tools/tool_curator.py`** — Tavily に加えて Reddit / YouTube の一次情報を拾う経路。
- 調査タスク一般 — Skill が入っていれば「〇〇を全網調研して」で自動的にルーティングされる。

いずれも**今回の導入では手を付けていない**。Agent Reach を入れただけの状態。

## 更新 / アンインストール

```bash
pipx upgrade agent-reach     # または agent-reach check-update で差分だけ確認
agent-reach uninstall        # 設定・トークン・SKILL.md を削除
```

## 参考

- リポジトリ: https://github.com/Panniantong/Agent-Reach
- 日本語 README: https://github.com/Panniantong/Agent-Reach/blob/main/docs/README_ja.md
- インストールガイド: https://github.com/Panniantong/agent-reach/blob/main/docs/install.md
