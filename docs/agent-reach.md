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

Agent Reach は **PyPI に無い**。GitHub から直接入れる。

```bash
py -m pip install --user pipx
py -m pipx ensurepath
pipx install git+https://github.com/Panniantong/agent-reach.git
```

`pipx` を使うのは、これが Bot のランタイム依存ではなく**開発者/エージェント用の CLI** だから。
`requirements.txt` には**入れない** (入れると Bot のデプロイが GitHub tarball を引きに行く)。

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
| `agent-reach doctor` の出力 | **中国語**。ログに混ざる前提で読む |
| `agent-reach skill --install` の書き込み先 | `~/.claude/skills/` (ユーザースコープ)。リポジトリの `.claude/skills/` ではない |

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
