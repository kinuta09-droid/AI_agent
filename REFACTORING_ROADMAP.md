# agent_hello.py 分割ロードマップ

**現状**: 2,673行の単一ファイル。以下の6つの論理ブロックに分割可能。

## 分割計画

| 優先度 | 抽出先 | 行範囲 | 行数 | 依存 |
|--------|--------|--------|------|------|
| 🟡 P1 | `tool_crypto.py` | L777-1607 | ~830行 | ccxt, pandas, pandas_ta, tavily_client, client |
| 🟡 P2 | `engine_plan_execute.py` | L1627-2117 | ~490行 | client, safe_json_loads, tools_map |
| 🟢 P3 | `lib_self_improve.py` | L2143-2673 | ~530行 | client, learning_db |
| ⚪ Low | utils 分離 | L84-184 | ~100行 | なし |

## 依存関係の課題

```
agent_hello.py の共有リソース:
├── client (Gemini API)
├── tavily_client (Search API)
├── safe_json_loads()
├── retry()
├── VAULT_ROOT, WORK_DIR 等の定数
└── tool_web_search() ← crypto / plan-execute が利用
```

### 推奨手順
1. **Step 1**: 共通リソースを `lib_shared.py` に抽出（client, tavily_client, 定数, ユーティリティ関数）
2. **Step 2**: `tool_crypto.py` を分離（`lib_shared` から import）
3. **Step 3**: `engine_plan_execute.py` を分離
4. **Step 4**: 各モジュールの動作検証
5. **Step 5**: `main.py` の tools_map を更新

### リスク
- 循環importの可能性（`agent_hello` ↔ 各ツール）
- `bot_discord.py` が `agent_hello` を直接importしている箇所がある
- スケジューラが `agent_hello.tool_*` を関数名で呼び出している

> ⚠️ この分割は **Plan Mode 必須タスク** です。`use subagents` で実行を推奨。
""", "CodeContent": "# agent_hello.py 分割ロードマップ

**現状**: 2,673行の単一ファイル。以下の6つの論理ブロックに分割可能。

## 分割計画

| 優先度 | 抽出先 | 行範囲 | 行数 | 依存 |
|--------|--------|--------|------|------|
| 🟡 P1 | `tool_crypto.py` | L777-1607 | ~830行 | ccxt, pandas, pandas_ta, tavily_client, client |
| 🟡 P2 | `engine_plan_execute.py` | L1627-2117 | ~490行 | client, safe_json_loads, tools_map |
| 🟢 P3 | `lib_self_improve.py` | L2143-2673 | ~530行 | client, learning_db |
| ⚪ Low | utils 分離 | L84-184 | ~100行 | なし |

## 依存関係の課題

```
agent_hello.py の共有リソース:
├── client (Gemini API)
├── tavily_client (Search API)
├── safe_json_loads()
├── retry()
├── VAULT_ROOT, WORK_DIR 等の定数
└── tool_web_search() ← crypto / plan-execute が利用
```

### 推奨手順
1. **Step 1**: 共通リソースを `lib_shared.py` に抽出（client, tavily_client, 定数, ユーティリティ関数）
2. **Step 2**: `tool_crypto.py` を分離（`lib_shared` から import）
3. **Step 3**: `engine_plan_execute.py` を分離
4. **Step 4**: 各モジュールの動作検証
5. **Step 5**: `main.py` の tools_map を更新

### リスク
- 循環importの可能性（`agent_hello` ↔ 各ツール）
- `bot_discord.py` が `agent_hello` を直接importしている箇所がある
- スケジューラが `agent_hello.tool_*` を関数名で呼び出している

> ⚠️ この分割は **Plan Mode 必須タスク** です。`use subagents` で実行を推奨。
