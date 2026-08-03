# -*- coding: utf-8 -*-
"""
Antigravity Plan-and-Execute Engine
agent_hello.py から抽出された実行エンジン。
Plan-and-Execute, Self-Improvement Protocol を提供する。
"""

import json
import os
import datetime
import inspect
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.genai import types

from lib.lib_shared import (
    client, MODEL_NAME, MODEL_NAME_HEAVY,
    safe_json_loads, load_json, save_json,
    LEARNING_DB_PATH, LEARNING_DB_ARCHIVE_PATH,
    PERFORMANCE_DASHBOARD_PATH, VAULT_ROOT,
    retry,
)
from lib.concurrency import fanout_workers, parallel_enabled
from lib.tool_registry import get_tools_config, get_parallel_safe_tools
from lib.cost_guard import GeminiBudgetExceeded
from lib.bot_errors import is_failure_result, PARTIAL_PREFIX

# スキル観測は任意機能: 不在・破損でもツール実行を止めない
try:
    from tools.tool_skill_observer import observe
except Exception as _observer_err:
    print(f"[Engine] ⚠️ tool_skill_observer unavailable ({_observer_err}) — running without observation.")

    def observe(tool_name, func, caller, **kwargs):
        return func(**kwargs)

# 長期記憶は任意機能: 不在でもプランナーを止めない
try:
    from tools.tool_user_memory import get_memory_block as _get_memory_block
except ImportError:
    def _get_memory_block(*args, **kwargs):
        return ""


@retry(max_retries=3, initial_delay=2)
def _gemini_call(model: str, prompt: str):
    """Gemini API 呼び出しを retry ラッパーで保護する共通ヘルパー。"""
    return client.models.generate_content(model=model, contents=prompt)


@retry(max_retries=3, initial_delay=2)
def _gemini_json_call(model: str, prompt: str, schema: dict = None) -> dict:
    """JSON mode / 構造化出力で Gemini を呼び出す共通ヘルパー。

    response_mime_type="application/json" によりJSON文法が保証されるため
    正規表現サルベージ(safe_json_loads)への依存を原則なくす。
    schema を渡すと response_schema でキー構造・enum まで強制できる。
    preview モデルが schema 非対応だった場合の保険として、
    parsed が取れないときのみ safe_json_loads にフォールバックする。
    """
    config_kwargs = {"response_mime_type": "application/json"}
    if schema is not None:
        config_kwargs["response_schema"] = schema
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    result = safe_json_loads(response.text)
    if not result:
        # 空dictを黙って返すと @retry が効かず、下流の critique["status"] 等で
        # KeyError になる。例外にして retry ラッパーに再試行させる
        raise ValueError(
            f"Gemini JSON response unparseable (model={model}): {str(response.text)[:200]}"
        )
    return result


# ---------------------------------------------------------
# Structured Output Schemas (動的enum: 有効ツールは起動構成で変動する)
# ---------------------------------------------------------

def _build_step_schema(valid_tools: list) -> dict:
    """plan / revised_plan の1ステップ分のスキーマ。

    tool を enum で制約し、存在しないツール名の提案を物理的に封じる。
    args_json は「計画時点で引数が確定できる場合のみ」プランナーが埋める
    JSON文字列 (引数はツール毎に任意キーのため、構造化出力では
    ネストobjectにできず文字列渡しにする。JSON modeが文法を保証済み)。
    """
    return {
        "type": "OBJECT",
        "properties": {
            "step": {"type": "INTEGER"},
            "task": {"type": "STRING"},
            "tool": {"type": "STRING", "enum": list(valid_tools)},
            "args_hint": {"type": "STRING"},
            "args_json": {"type": "STRING"},
        },
        "required": ["step", "task", "tool"],
    }


def _build_plan_schema(valid_tools: list) -> dict:
    """generate_plan() 用のレスポンススキーマ。"""
    return {
        "type": "OBJECT",
        "properties": {
            "plan": {"type": "ARRAY", "items": _build_step_schema(valid_tools)},
            "expected_outcome": {"type": "STRING"},
        },
        "required": ["plan", "expected_outcome"],
    }


def _build_critique_schema(valid_tools: list) -> dict:
    """critique_results() 用のレスポンススキーマ。status を enum で強制する。"""
    return {
        "type": "OBJECT",
        "properties": {
            "status": {"type": "STRING", "enum": ["ok", "needs_revision", "failed"]},
            "feedback": {"type": "STRING"},
            "revised_plan": {"type": "ARRAY", "items": _build_step_schema(valid_tools)},
        },
        "required": ["status", "feedback"],
    }


# ---------------------------------------------------------
# Plan-and-Execute Engine
# ---------------------------------------------------------

def _get_tools_config():
    """ツール一覧と実行マップを返す（lib.tool_registry へ委譲）。"""
    return get_tools_config()


def generate_plan(user_query: str, session_context: str = "") -> dict:
    """ユーザーのクエリから実行計画を生成する。

    Args:
        user_query: ユーザーの要求
        session_context: 直前タスクの文脈テキスト (空なら注入しない)。
            整形はこの関数側で行うため、呼び出し元は素のテキストを渡すだけでよい。

    Returns:
        {
            "plan": [
                {"step": 1, "task": "...", "tool": "...", "args_hint": "..."},
                ...
            ],
            "expected_outcome": "最終的に何が達成されるか"
        }
    """
    config_tools, _ = _get_tools_config()
    valid_tools = [t["name"] for t in config_tools]
    tools_desc = "\n".join(
        f"- {t['name']}: {t['description']}  使い方: {t['usage']}"
        for t in config_tools
    )

    # Self-Improvement: 過去の学習からヒントを注入
    learning_hint = get_enhanced_prompt(user_query)
    learning_block = f"\n\n{learning_hint}" if learning_hint else ""

    # 長期記憶: ユーザーに関する事実を注入 (learning_block と同型のパターン)
    memory_hint = _get_memory_block()
    memory_block = f"\n\n{memory_hint}" if memory_hint else ""

    # 短期セッション記憶: 直前タスクの文脈を注入 (learning_block と同型のパターン)
    session_block = ""
    if session_context:
        session_block = f"""

【直前のタスク(参考)】
{session_context}
※ 要求が「それ」「さっき」「続き」「同じ」など直前の作業を参照している場合のみ、
  この文脈を使って解釈すること。無関係な要求の場合は完全に無視する。"""

    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")

    prompt = f"""あなたはタスクプランナーです。
ユーザーの要求を達成するための実行計画を作成してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などの表現は、上記の日付を基準にしてください。

【要求】: {user_query}

【利用可能なツール】:
{tools_desc}

【ルール】
- 各ステップは1つのツール呼び出しに対応させる
- 情報収集 → 加工・保存 → 完了報告 の順序を守る
- ニュース収集には tool_fetch_daily_news のみを使い、tool_create_note での二重作成は禁止
- レポート・要約・分析の作成を求められた場合、必ず tool_create_note で30_Mainに保存する
- 必要なステップだけに絞り、冗長な計画にしない（最大6ステップ）

【制約の抽出と遵守】（重要！）
ユーザーの要求から以下のパターンで「禁止事項」を抽出し、厳守してください：
- 「〜せずに」「〜なしで」「〜を使わないで」「〜は禁止」「〜を避けて」
  → 該当するツールや行動を計画から除外する
- 例: 「Web検索せずに」→ tool_web_search, tool_fetch_daily_news, tool_fetch_url を使用禁止
- 例: 「ファイルを作成せずに」→ tool_create_note を使用禁止

【制約がある場合の代替案】（必ず代替手段を探す！空のplanは禁止！）
- Web検索禁止で「ニュース」を求められた場合:
  Step 1: tool_vault_hybrid_search(query="news ニュース") で過去のニュースファイルを探す
  Step 2: tool_read_note でそのファイルを読んで内容を返す
- ファイル作成禁止の場合: 結果をテキストで返すだけにする
- **重要**: 代替手段がある限り、必ず1つ以上のステップを持つ計画を作成すること
- 「実行不可」と返すのは本当にどうしようもない場合のみ

【ツール選択の指針】
- **外部情報が必要な場合** (最新ニュース、一般知識、技術情報など): tool_web_search を使う
- **「今日/本日/急に」等、直近の出来事の正確な理由・数値が特に重要な場合**:
  まず tool_web_search (recency_days付き) を試す。それでも精度が不安な場合や、
  Criticから再検索を求められた場合の代替として tool_web_search_grounded を使う
  (Google自身の検索インデックスを使うため精度は高いが、日次呼び出し数に別枠の
  上限があるため無闇に最初から選ばない)
- **ユーザーの過去のメモや文脈が必要な場合**: tool_vault_hybrid_search を使う (キーワード+意味検索を自動統合)
- **両方が必要な場合**: まず tool_web_search で外部情報を取得し、その後 tool_vault_hybrid_search で関連する過去ノートを探す
- **URLが与えられた場合**: tool_fetch_url でページ内容を取得する
- **既存ファイルに文章を書き足す場合**: tool_update_note を使う
  (tool_sync_to_daily_note は予定/タスクの同期専用で、任意のテキストは書けない)

【args_json の出力ルール】（実行を高速化するため重要！）
- 引数がこの時点で確定できるステップは、args_json に引数のJSON文字列を出力すること
  - 例: Step 1 の検索 → args_json: "{{\\"query\\": \\"AI 最新トレンド {today_str[:4]}\\"}}"
  - 例: 引数不要ツール → args_json: "{{}}"
- 前のステップの実行結果に依存する引数（検索結果を元にしたレポート本文など）は
  args_json を空文字にして args_hint だけ書くこと（実行時に解決される）
{learning_block}{memory_block}{session_block}"""

    return _gemini_json_call(MODEL_NAME, prompt, _build_plan_schema(valid_tools))


def _try_planned_args(item: dict) -> dict:
    """plan項目の args_json が使えるなら dict を返す。使えなければ None。

    None の場合は従来通り _resolve_tool_args() (Gemini呼び出し) で解決する。
    プランナーが計画時点で引数を確定できたステップはここで拾われ、
    ステップ毎の直列Gemini呼び出しが丸ごとスキップされる。
    """
    raw = item.get("args_json", "")
    if not raw or not str(raw).strip():
        return None
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    # tool_create_note は本文が前ステップの結果に依存するため、
    # 必須キーが揃っていない事前確定引数は信用せず resolve に回す
    if item.get("tool") == "tool_create_note" and not (
        "filename" in args and "content" in args
    ):
        return None
    return args


# Gemini が「1ステップ分の引数」ではなく計画全体やラッパー構造を吐いたときに
# 現れるキー。例: args_json = '{"plan": [{"tool_read_note": {...}}, ...]}'
# → tool_read_note(plan=[...]) となり TypeError で落ちていた。
_ARG_WRAPPER_KEYS = frozenset({
    "plan", "args", "arguments", "kwargs",
    "parameters", "params", "tool_input", "input",
})


def _tool_param_spec(func):
    """ツール関数の (受理キー, 必須キー, **kwargs有無) を返す。

    シグネチャが取れない/キーワードで渡せない形（位置専用引数）の場合は
    None を返し、呼び出し側は検証をスキップして素通しする。
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None

    accepted, required, has_var_kw = set(), set(), False
    for name, p in sig.parameters.items():
        if p.kind is p.VAR_KEYWORD:
            has_var_kw = True
        elif p.kind is p.VAR_POSITIONAL:
            continue
        elif p.kind is p.POSITIONAL_ONLY:
            # ** 展開で渡せないため、この関数は検証対象外とする
            return None
        else:
            accepted.add(name)
            if p.default is p.empty:
                required.add(name)
    return accepted, required, has_var_kw


def _sanitize_tool_args(tool_name: str, func, args, _depth: int = 0):
    """func(**args) が TypeError にならない引数dictを返す。使えなければ None。

    None を返した場合、呼び出し側は Gemini による再解決へフォールバックする。
    やること:
      1. dict 以外 / 非文字列キー → 拒否（** 展開できない）
      2. {"args": {...}} 等のラッパー1枚を剥がす
      3. シグネチャに無いキーを捨てる（**kwargs を持つツールは素通し）
      4. 必須引数が欠けていたら拒否
    """
    if not isinstance(args, dict):
        return None
    if any(not isinstance(k, str) for k in args):
        return None

    spec = _tool_param_spec(func)
    if spec is None:
        return args  # 検証不能 → 従来通り素通し
    accepted, required, has_var_kw = spec

    # ラッパー剥がし: 単一キーがラッパー名かつ中身がdictのときだけ1段潜る。
    # {"plan": [...]} のように中身がlistの場合はどのステップの引数か判別
    # できないため剥がさず拒否し、args_hint 付きの再解決に任せる。
    if (
        _depth < 2
        and len(args) == 1
        and not (set(args) & accepted)
        and not has_var_kw
    ):
        key, value = next(iter(args.items()))
        if key in _ARG_WRAPPER_KEYS:
            if isinstance(value, dict):
                return _sanitize_tool_args(tool_name, func, value, _depth + 1)
            return None

    if has_var_kw:
        filtered = dict(args)
    else:
        filtered = {k: v for k, v in args.items() if k in accepted}
        dropped = set(args) - set(filtered)
        if dropped:
            print(f"[P&E] ⚠️ {tool_name}: 未知の引数を除去 {sorted(dropped)}")

    missing = required - set(filtered)
    if missing:
        print(f"[P&E] ⚠️ {tool_name}: 必須引数が不足 {sorted(missing)}")
        return None
    return filtered


def _resolve_step_args(tool_name: str, func, item: dict, task: str,
                       context: list):
    """1ステップ分の引数を決める。計画時の args_json → 検証 → NGなら Gemini解決。

    どちらの経路でも検証を通せなければ None（＝ステップを明示的に失敗させる）。
    """
    planned = _try_planned_args(item)
    if planned is not None:
        args = _sanitize_tool_args(tool_name, func, planned)
        if args is not None:
            return args
        print(f"[P&E] ⚠️ {tool_name}: 計画時の args_json が不正 → Gemini再解決")

    resolved = _resolve_tool_args(tool_name, item.get("args_hint", ""),
                                  task, context)
    return _sanitize_tool_args(tool_name, func, resolved)


def _is_fanout_ready(item: dict, tools_map: dict, parallel_safe) -> bool:
    """このステップを他ステップと同時に実行してよいか (Phase C1)。

    2つの条件を **両方** 満たすときだけ True:

    1. データ依存が無い — `_try_planned_args()` が使える引数を返すこと。
       これは新しい判定ではなく、既存の契約をそのまま読んでいる:
       generate_plan のプロンプトは「前のステップの実行結果に依存する引数は
       args_json を空文字にして args_hint だけ書く」と指示している。
       つまり args_json が使えるステップは、定義上どの前ステップの
       observation も必要としていない。
       Planner に depends_on を書かせない理由: LLM が依存を1本落とすと
       競合状態が黙って混入する。落とし方が「安全側」にならない判定は使わない。

    2. 副作用が無い — レジストリで parallel_safe が宣言されていること。
       データ依存が無くても、書き込み順は保たれている必要がある
       (ノートを作ってから読む、等)。

    _sanitize_tool_args まで通すのは、ここで OK と言ったステップが実行時に
    Gemini 再解決へ落ちると「依存が無い」という前提が崩れるため
    (再解決は accumulated_context を読む = 他ステップの結果に依存する)。
    """
    tool_name = item.get("tool", "")
    if tool_name not in parallel_safe:
        return False
    func = tools_map.get(tool_name)
    if func is None:
        return False
    planned = _try_planned_args(item)
    if planned is None:
        return False
    return _sanitize_tool_args(tool_name, func, planned) is not None


def _plan_waves(plan: list, tools_map: dict, parallel_safe=None,
                enabled=None) -> list:
    """計画を実行単位 (ウェーブ) に分割する純関数。

    連続する fan-out 可能なステップを1つのウェーブに束ね、それ以外は
    1要素ウェーブ (= 従来どおりの逐次) にする。

    「連続する」に限る理由:
      間に副作用のあるステップを挟んで並べ替えると、計画の書き順が持っていた
      意味 (保存してから読む、等) が壊れる。ウェーブ内の結果は元のステップ順に
      並べ直すので、**results の中身と順序は逐次実行と同一**になる。違うのは
      タイミングだけ。

    parallel_safe / enabled を注入できるようにしてあるのはテスト用。
    os.environ を書き換えて切り替える方式は採らない (CLAUDE.md スコープ階層 LEVEL 2)。
    """
    if parallel_safe is None:
        parallel_safe = get_parallel_safe_tools()
    if enabled is None:
        enabled = parallel_enabled()

    if not enabled:
        return [[item] for item in plan]

    waves, current = [], []
    for item in plan:
        if _is_fanout_ready(item, tools_map, parallel_safe):
            current.append(item)
            continue
        if current:
            waves.append(current)
            current = []
        waves.append([item])
    if current:
        waves.append(current)
    return waves


def _run_step(item: dict, tools_map: dict, context: list,
              fallback_step: int = 1) -> dict:
    """1ステップを実行して step_result を返す。**例外を外へ出さない**。

    step_callback を呼ばないのは意図的: ウェーブ内ではこれがワーカースレッドから
    呼ばれるため、進捗通知はオーケストレータ側 (execute_plan) に集約する。
    ワーカーから直接飛ばすと、3本同時のとき Discord の edit が6回ほぼ同時に走って
    レート制限に当たる。

    context は呼び出し側が渡したスナップショット。fan-out されたステップは
    定義上これを読まない (_is_fanout_ready の条件1) が、逐次ステップは読む。
    """
    step_num = item.get("step", fallback_step)
    task = item.get("task", "")
    tool_name = item.get("tool", "")
    args_hint = item.get("args_hint", "")
    started = time.time()

    print(f"[P&E] ▶️ Step {step_num}: {task} (tool={tool_name})")

    # 引数検証にシグネチャが要るため、ツール解決を先に行う
    func = tools_map.get(tool_name)
    args = {}
    try:
        if func is None:
            observation = f"Error: Unknown tool '{tool_name}'"
            success = False
        else:
            # 計画時点で確定済みの引数があれば使い、なければ Gemini に解決させる。
            # どちらもシグネチャ検証を通す（TypeError を事前に潰す）
            args = _resolve_step_args(tool_name, func, item, task, context)
            if args is None:
                args = {}
                observation = (
                    f"Error: '{tool_name}' の引数を解決できませんでした "
                    f"(hint: {args_hint or 'なし'})"
                )
                success = False
            else:
                # tool_create_note の場合、content を専用プロンプトでリッチに再生成する
                # JSON制約内では長文生成が困難なため、2段階方式で品質を担保する
                if tool_name == "tool_create_note" and context:
                    rich_content = _generate_rich_content(
                        task=task,
                        filename=args.get("filename", ""),
                        context=context,
                    )
                    if rich_content:
                        args["content"] = rich_content

                try:
                    observation = observe(tool_name, func, "engine_plan_execute", **args)
                    # ツール層は例外を投げず "Error: ..." 文字列を返す規約なので、
                    # 「例外が出なかった＝成功」と決めつけない。決めつけていた間、
                    # 失敗したステップが Critic には成功として渡り、learning_db には
                    # outcome="success" で残り、Discord には ✅ Mission Complete が
                    # 出ていた (2026-07-27 に評価ハーネスで実測)。
                    # 判定は lib.bot_errors に集約する (二重管理は必ず割れる)。
                    success = not is_failure_result(observation)
                    if not success:
                        print(f"[P&E] ⚠️ {tool_name} が文字列で失敗を返しました "
                              f"(例外なし) → step {step_num} を failed として扱います")
                except Exception as e:
                    observation = f"Error: {e}"
                    success = False
    except Exception as e:
        # ここに来るのは引数解決やリッチ本文生成が想定外の例外を投げたとき。
        # ワーカースレッドで投げっぱなしにすると future.result() で
        # execute_plan ごと落ち、**同ウェーブの実行済みの結果まで消える**。
        observation = f"Error ({type(e).__name__}): {e}"
        success = False
        print(f"[P&E] ❌ Step {step_num} が例外で終了: {observation}")

    print(f"  {'✅' if success else '❌'} {str(observation)[:200]}...")

    return {
        "step": step_num,
        "task": task,
        "tool": tool_name,
        "args": args,
        "observation": observation,
        "success": success,
        "duration_seconds": round(time.time() - started, 2),
    }


def _run_wave(wave: list, tools_map: dict, accumulated_context: list) -> tuple:
    """ウェーブを実行し (計画順に並べた結果, 短縮できた秒数) を返す。

    短縮秒数を「ステップ所要時間の合計 − ウェーブの実測壁時計」で **実行の中から**
    取るのは、別実行との A/B にしないため。このプロジェクトでは
    「before を別の日・別の索引状態で取ったせいで施策の効果と混ざった」という
    失敗を既に踏んでいる (Phase B の回帰ゲート)。同一実行内なら土俵が同じ。

    **この数字の既知のバイアス** (数値と一緒に持ち回ること):
      各ステップの duration_seconds には、共有資源の待ち時間が含まれる。
      特に lib.concurrency.ollama_slot は Ollama 呼び出しを直列化するので、
      Vault 検索を2本同時に流すと後発の待ち時間が自分の所要時間として計上され、
      「逐次なら」が実際より大きく出る (= saved_seconds は過大評価)。
      資源を奪い合わないステップ (Web検索×3 など) では正確。
      **この値を「最低でもこれだけ速くなった」と読んではいけない。**
    """
    if len(wave) == 1:
        return [_run_step(wave[0], tools_map, accumulated_context)], 0.0

    # ワーカーには文脈のスナップショットを渡す。fan-out 対象は定義上これを
    # 読まないが、共有リストを実行中に触らせない方が後から読む人が迷わない。
    snapshot = list(accumulated_context)
    workers = min(len(wave), fanout_workers())
    print(f"[P&E] ⚡ Wave: {len(wave)}件を並列実行します (worker={workers}) — "
          + " / ".join(str(i.get('tool', '?')) for i in wave))

    started = time.time()
    indexed = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_step, item, tools_map, snapshot,
                            item.get("step", pos + 1)): pos
            for pos, item in enumerate(wave)
        }
        for fut in as_completed(futures):
            pos = futures[fut]
            try:
                indexed[pos] = fut.result()
            except BaseException as e:
                # _run_step は例外を外へ出さない契約だが、契約が破れたときに
                # **ウェーブ全体の結果を道連れにしない**。ここで再送出すると
                # executor の __exit__ を抜けた時点で execute_plan ごと落ち、
                # 既に完了している他ステップの結果まで消える。
                item = wave[pos]
                indexed[pos] = {
                    "step": item.get("step", pos + 1),
                    "task": item.get("task", ""),
                    "tool": item.get("tool", ""),
                    "args": {},
                    "observation": f"Error ({type(e).__name__}): {e}",
                    "success": False,
                    "duration_seconds": 0.0,
                }
                print(f"[P&E] ❌ ワーカーが結果を返せませんでした: {indexed[pos]['observation']}")

    wall = time.time() - started
    ordered = [indexed[pos] for pos in range(len(wave))]
    sequential = sum(r.get("duration_seconds", 0.0) for r in ordered)
    saved = max(sequential - wall, 0.0)
    speedup = (sequential / wall) if wall > 0 else 1.0
    print(f"[P&E] ⚡ Wave({len(wave)}件): 逐次なら {sequential:.1f}秒 → "
          f"実測 {wall:.1f}秒 ({speedup:.1f}x, -{saved:.1f}秒)")
    return ordered, saved


def _emit_wave_event(step_callback, wave: list, total: int, status: str,
                     results: list = None) -> None:
    """ウェーブ1本につき1回だけ進捗を通知する。

    step / task / tool に**先頭ステップ**の値を入れるのは後方互換のため
    (既存の描画は step_data["step"] を読む)。ウェーブの全容は "wave" キーに入れ、
    描画側が長さ1かどうかで出し分ける。
    """
    if not step_callback:
        return
    head = wave[0] if wave else {}
    event = {
        "phase": "execute",
        "step": head.get("step", "?"),
        "total": total,
        "task": head.get("task", ""),
        "tool": head.get("tool", ""),
        "status": status,
        "wave": [{"step": i.get("step", "?"), "task": i.get("task", ""),
                  "tool": i.get("tool", "")} for i in wave],
    }
    if results is not None:
        # 失敗が1件でもあれば error。ウェーブ全体を1行で出すので、
        # 「一部失敗を ✅ で表示しない」という既存の規約をここでも守る。
        failed = [r for r in results if not r.get("success")]
        event["status"] = "error" if failed else "done"
        # observation は **status と同じステップのもの** を載せる。
        # 常に results[0] にすると、status="error" のイベントが成功した
        # 1件目の本文を持つ = 「失敗と言いながら成功の中身を見せる」ことになる。
        # 今の描画は observation を使わないが、食い違ったデータを流すこと自体が
        # このプロジェクトが何度も潰してきた「表示が実態と食い違う」の種になる。
        source = failed[0] if failed else (results[0] if results else {})
        event["observation"] = str(source.get("observation", ""))[:500]
        event["failed_steps"] = [r.get("step") for r in failed]
    step_callback(event)


def execute_plan(plan: list, step_callback=None) -> dict:
    """計画を実行する。依存の無い連続ステップは同時に走らせる (Phase C1)。

    Args:
        plan: generate_plan() が返した plan リスト
        step_callback: Discord等へのリアルタイム通知用コールバック

    Returns:
        {"success": bool, "results": [...], "final_output": "...",
         "parallel": {"waves": int, "saved_seconds": float}}

    失敗したら **ウェーブ境界で** 止める (従来の「最初の失敗で break」を
    ウェーブ単位に読み替えたもの)。ウェーブ内で失敗が出ても同ウェーブの
    他ステップの結果は捨てない — 既に実行済みなので、捨てる方が実態に反する。
    失敗の局所化 (依存していない後続ステップを続行する) は Phase C2 の範囲。
    """
    _, tools_map = _get_tools_config()
    results = []
    accumulated_context = []
    total = len(plan)
    parallel = {"waves": 0, "saved_seconds": 0.0}

    for wave in _plan_waves(plan, tools_map):
        _emit_wave_event(step_callback, wave, total, "running")

        wave_results, saved = _run_wave(wave, tools_map, accumulated_context)
        if len(wave) > 1:
            parallel["waves"] += 1
            parallel["saved_seconds"] = round(parallel["saved_seconds"] + saved, 2)

        results.extend(wave_results)
        accumulated_context.extend(wave_results)

        _emit_wave_event(step_callback, wave, total, "done", wave_results)

        failed = [r for r in wave_results if not r["success"]]
        if failed:
            steps = "、".join(str(r["step"]) for r in failed)
            print(f"[P&E] ⚠️ Step {steps} failed, passing to Critic...")
            break

    all_ok = all(r["success"] for r in results)
    return {
        "success": all_ok,
        "results": results,
        "final_output": _summarize_results(results) if all_ok else "",
        "parallel": parallel,
    }


def _resolve_tool_args(tool_name: str, args_hint: str, task: str,
                        context: list) -> dict:
    """Gemini を使ってツールの引数を具体化する。"""
    if not args_hint and tool_name in (
        "tool_merge_weekly_reports", "tool_merge_all_weekly_reports", "tool_fetch_daily_news",
        "tool_scan_crypto_market", "tool_audit_predictions",
        "tool_curate_topics",
    ):
        return {}

    # ツールのシグネチャ（必須引数情報）を取得
    config_tools, _ = _get_tools_config()
    tool_usage = ""
    for t in config_tools:
        if t["name"] == tool_name:
            tool_usage = t["usage"]
            break

    ctx_summary = ""
    if context:
        ctx_summary = "\n".join(
            f"Step {c['step']}: {c['task']} → {str(c['observation'])[:1500]}"
            for c in context[-3:]
        )

    _now = datetime.datetime.now()
    today_str = _now.strftime("%Y年%m月%d日")
    today_ym = f"{_now.year}年{_now.month}月"

    prompt = f"""以下のツール呼び出しに必要な引数をJSON辞書で出力してください。

【現在の日付】: {today_str}
※ 「最新」「今年」などは{today_str[:4]}年を指します。検索クエリにも正しい年を使用してください。

ツール: {tool_name}
シグネチャ: {tool_usage}
タスク: {task}
引数ヒント: {args_hint}

これまでの実行結果（引数の値はここから取得してください）:
{ctx_summary}

【重要な注意事項】
- シグネチャにある引数は必ず全て含めてください
- tool_web_search の場合:
  - query: 「最新」と言われたら {today_str[:4]}年 を含めてください（例: "AI 最新トレンド {today_str[:4]}"）
  - recency_days: タスクが「今日」「本日」「急に」「最近」「直近」のように
    直近の出来事の理由・変化を尋ねている場合は 7〜14 を指定してください。
    一般的な知識・技術解説・過去の経緯を尋ねる質問では指定不要です
    （不要な質問にまで付けると直近ニュースに偏り、逆に精度が落ちます）
  - recency_days を付ける場合、query にも {today_ym} を含めてください
    （例: "ガソリン 値下がり 理由 {today_ym}"）。直近のニュース記事は
    タイトルに更新月がそのまま入っていることが多く（例:「【2026年7月31日
    更新】」）、クエリに月まで入れるとTavilyの検索順位で優先されやすく
    なります（実測: 月まで含めた場合は該当記事が検索結果の先頭付近、
    年だけ・月なしの場合は無関係な記事が上位に来た）
- tool_create_note の場合:
  - filename: レポートのタイトル（日本語OK）
  - content: 以下の品質基準を満たす充実したレポート本文を生成すること
    ★ これまでの実行結果（observation）に含まれる具体的な情報を全て活用する
    ★ 固有名詞、数値データ、人名、日付、URL等は省略せず含める
    ★ 見出し（##）、箇条書き、太字を使って構造化する
    ★ 最低でも500文字以上の本文を生成する（短すぎるレポートは不可）
    ★ 「概要」「詳細」「まとめ」などのセクションに分けて整理する
  - summary: 1-2行の要約
- 引数の値は「これまでの実行結果」の observation から抽出・生成してください
- 引数が不要なら空の辞書 {{}} を返してください

JSONのみ出力（説明不要）:"""

    for attempt in range(3):
        try:
            # スキーマなしJSON mode: 引数はツール毎に任意キーのため構造は縛らない
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
            args = safe_json_loads(response.text)
            # 必須引数チェック (tool_create_note)
            if tool_name == "tool_create_note":
                if "filename" not in args or "content" not in args:
                    print(f"[P&E] ⚠️ Missing required args for tool_create_note, retry {attempt+1}/3")
                    continue
            return args
        except Exception as e:
            print(f"[P&E] ⚠️ _resolve_tool_args error: {e}, retry {attempt+1}/3")
    # 最終手段: デフォルト引数
    if tool_name == "tool_create_note":
        return {"filename": "Report.md", "content": "生成に失敗しました", "summary": ""}
    return {}


def _generate_rich_content(task: str, filename: str, context: list) -> str:
    """tool_create_note 用のリッチなレポート本文を専用プロンプトで生成する。

    JSON制約から解放された自由形式で、充実したMarkdownレポートを生成する。
    """
    ctx_text = "\n\n".join(
        f"--- Step {c['step']}: {c['task']} ---\n{str(c['observation'])[:3000]}"
        for c in context[-5:]
    )

    prompt = f"""あなたは優秀なリサーチャーです。以下の調査結果を元に、Obsidianノート用の充実したレポート本文を作成してください。

【タスク】: {task}
【ファイル名】: {filename}

【調査結果（これを元にレポートを作成）】:
{ctx_text}

【レポート作成ルール】
1. Markdown形式で構造化すること（##見出し、箇条書き、**太字**を活用）
2. 以下のセクション構成を推奨:
   - ### 1. 概要と背景
   - ### 2. 詳細分析（メインの調査内容）
   - ### 3. 特徴・ポイント
   - ### 4. 最新動向と今後の展望
3. 調査結果に含まれる具体的な情報は全て含めること:
   - 固有名詞（人名、企業名、サービス名）
   - 数値データ（金額、人数、日付、パーセンテージ）
   - 重要な引用や事実関係
4. 最低1500文字以上の本文を生成すること
5. 抽象的・一般論的な記述ではなく、調査結果の具体的データに基づいた記述にする
6. フロントマター（---）は付けない。純粋な本文のみ出力する

レポート本文を出力してください:"""

    try:
        response = _gemini_call(MODEL_NAME_HEAVY, prompt)
        content = response.text.strip()
        # 最低限の品質チェック: 短すぎる場合はNoneを返してフォールバック
        if len(content) < 300:
            print(f"[P&E] ⚠️ Rich content too short ({len(content)} chars), falling back to args content")
            return None
        print(f"[P&E] 📝 Generated rich content: {len(content)} chars")
        return content
    except Exception as e:
        print(f"[P&E] ⚠️ _generate_rich_content failed: {e}")
        return None


def _summarize_results(results: list) -> str:
    """実行結果リストから最終サマリーを生成する (Gemini 生成が失敗したときの代替)。

    従来は「Success: / ✅ で始まらない observation」を一律 "完了" と書いていたため、
    失敗ステップが完了として報告され、しかもエラー本文が捨てられていた。
    execute_plan が success に is_failure_result を通すようになった (2026-07-27) ため
    文字列判定は原則冗長だが、results は他所からも組み立てられる (テスト・将来の
    別エンジン) ので、同じ関数で二重に見ておく。冗長でも判定は1本。
    """
    parts = []
    for r in results:
        obs = str(r.get("observation", ""))
        if not r.get("success", True) or is_failure_result(obs):
            parts.append(f"- Step {r['step']} ({r['task']}): ❌ 失敗 — {obs[:200]}")
        elif obs.startswith("Success:") or obs.startswith("✅"):
            parts.append(f"- Step {r['step']}: {obs[:200]}")
        else:
            parts.append(f"- Step {r['step']} ({r['task']}): 完了")
    return "\n".join(parts)


def critique_results(user_query: str, plan: list, results: list) -> dict:
    """実行結果を評価し、必要なら修正計画を返す。

    Returns:
        {
            "status": "ok" | "needs_revision" | "failed",
            "feedback": "フィードバック内容",
            "revised_plan": [...] (needs_revision の場合のみ)
        }
    """
    config_tools, _ = _get_tools_config()
    valid_tools = [t["name"] for t in config_tools]
    valid_tools_str = ", ".join(valid_tools)

    results_summary = json.dumps(
        [{"step": r["step"], "task": r["task"], "success": r["success"],
          "observation": str(r["observation"])[:1500]}
         for r in results],
        ensure_ascii=False, indent=2,
    )

    plan_summary = "\n".join(
        f"  {p.get('step', i+1)}. {p.get('task', '')} [{p.get('tool', '')}]"
        for i, p in enumerate(plan)
    )

    prompt = f"""あなたはタスク評価者（Critic）です。
実行結果を評価し、ユーザーの目標が達成されたか判定してください。

【ユーザーの要求】: {user_query}

【元の計画】:
{plan_summary}

【実行結果】:
{results_summary}

【利用可能なツール（これ以外は使用禁止）】:
{valid_tools_str}

【ユーザー制約の遵守】（revised_plan作成時に厳守！）
- ユーザーの要求に「〜せずに」「〜なしで」「〜を使わないで」「〜禁止」があれば、該当ツールは使用禁止
- 例: 「Web検索せずに」→ revised_plan に tool_web_search, tool_fetch_daily_news, tool_fetch_url を含めてはいけない
- 代替手段がある場合は必ずその代替案で revised_plan を作成すること

【評価基準】
1. 全ステップが成功しているか
2. ユーザーの目標が達成されたか（情報が十分か、ファイルが作成されたか等）
3. **コンテンツ品質チェック**（最重要）:
   - 「調べて」「まとめて」系の要求 → observationに具体的な情報（手順、数字、固有名詞）が含まれているか？
   - 一般論や概要だけで具体性がない場合 → needs_revision（「具体的な手順/データを追加検索せよ」と指示）
   - 例: 「ストレッチの方法を調べて」→ 具体的なストレッチ名・やり方・回数がなければ不十分
   - **tool_web_search (Tavily) で「今日/本日/急に」等の直近の出来事を調べたが
     一次情報が具体的でない場合**: revised_planでの再検索には
     tool_web_search_grounded (Google Search Grounding、精度優先) を提案する
4. 不足があれば、追加で必要なステップを提案
- **重要**: revised_planで提案するツールは上記の利用可能なツールのみ使用可能
- **重要**: revised_planはユーザーの制約を違反してはいけない

【revision判断の基準】（時間効率を考慮！）
- 全ステップ成功 + 具体的な情報が含まれている → status=ok（revisionしない）
- ステップ失敗あり → needs_revision
- 全ステップ成功だが情報が薄い → needs_revision（ただし追加ステップは最大2つまで）
- 同じツールの再実行は避け、別の検索クエリや別ツールで補完すること

【statusの意味】
- status=ok: 目標達成、追加作業不要（revised_planは空配列にする）
- status=needs_revision: 部分的に成功、追加ステップが必要（revised_planに記載、最大2ステップ）
- status=failed: 根本的に失敗、リトライ不可"""

    try:
        return _gemini_json_call(
            MODEL_NAME, prompt, _build_critique_schema(valid_tools)
        )
    except Exception as e:
        return {"status": "ok", "feedback": f"Critic evaluation failed ({e}), accepting results."}


MAX_REVISIONS = 1  # 品質向上のため1回に制限（2回は時間浪費の原因）


def run_plan_and_execute(user_query: str, step_callback=None,
                         session_context: str = "") -> str:
    """Plan-and-Execute パターンのメインエントリーポイント。

    session_context: 直前タスクの文脈 (短期セッション記憶)。generate_plan にパススルー。
    デフォルト空文字なので既存呼び出し元 (scheduler / main.py 等) は無影響。

    1. generate_plan() で計画を作成
    2. execute_plan() で実行
    3. critique_results() で評価
    4. 必要なら再計画・再実行（最大 MAX_REVISIONS 回）
    5. log_execution() で学習DBに記録
    6. 最終結果を返す
    """
    t_start = time.time()
    print(f"[P&E] 🧠 Planning for: {user_query[:80]}...")

    if step_callback:
        step_callback({"phase": "plan", "status": "planning"})

    try:
        plan_data = generate_plan(user_query, session_context=session_context)
    except GeminiBudgetExceeded as e:
        # 日次予算超過は障害ではない — 完成済みの日本語文言をそのまま返す
        print(f"[P&E] ⛔ Budget exceeded: {e}")
        return str(e)
    except Exception as e:
        print(f"[P&E] ❌ Plan generation failed: {e}")
        return f"計画生成に失敗しました: {e}"

    plan = plan_data.get("plan", [])
    original_plan = list(plan)
    expected = plan_data.get("expected_outcome", "")

    if not plan:
        return "計画を生成できませんでした。"

    print(f"[P&E] 📋 Plan ({len(plan)} steps): {expected}")
    for p in plan:
        print(f"  {p['step']}. {p['task']} [{p['tool']}]")

    if step_callback:
        step_callback({
            "phase": "plan",
            "status": "ready",
            "plan": plan,
            "expected_outcome": expected,
        })

    all_results = []
    last_critique = {}
    # 修正計画をまたいで積む (1回の /agent で fan-out が何回効いたかを見たい)
    parallel_total = {"waves": 0, "saved_seconds": 0.0}

    def _log(critique: dict) -> None:
        """学習DBへの記録。4箇所で同じ try/except を書き写さないための1本化。"""
        try:
            log_execution(user_query, original_plan, all_results, critique,
                          time.time() - t_start, parallel=parallel_total)
        except Exception as e:
            print(f"[P&E] ⚠️ Failed to log execution: {e}")

    for revision in range(MAX_REVISIONS + 1):
        if revision > 0:
            print(f"[P&E] 🔄 Revision {revision}/{MAX_REVISIONS}")
            if step_callback:
                step_callback({"phase": "revision", "attempt": revision})

        exec_result = execute_plan(plan, step_callback)
        all_results.extend(exec_result["results"])
        wave_stats = exec_result.get("parallel") or {}
        parallel_total["waves"] += wave_stats.get("waves", 0)
        parallel_total["saved_seconds"] = round(
            parallel_total["saved_seconds"] + wave_stats.get("saved_seconds", 0.0), 2)

        if step_callback:
            step_callback({"phase": "critique", "status": "evaluating"})

        critique = critique_results(user_query, plan, exec_result["results"])
        last_critique = critique
        print(f"[P&E] 🔍 Critic: {critique.get('status')} - {critique.get('feedback', '')[:100]}")

        if critique["status"] == "ok":
            _log(critique)
            return _final_report(user_query, all_results, critique)
        elif critique["status"] == "needs_revision":
            revised = critique.get("revised_plan", [])
            if revised:
                plan = revised
                print(f"[P&E] 📝 Revised plan ({len(plan)} steps)")
            else:
                _log(critique)
                return _final_report(user_query, all_results, critique)
        else:
            _log(critique)
            return f"タスク失敗: {critique.get('feedback', 'Unknown error')}"

    _log(last_critique)
    return _final_report(user_query, all_results, last_critique)


def _final_report(user_query: str, results: list, critique: dict) -> str:
    """最終的な戻り値を作る。1ステップも成功していない実行を成功の文面で返さない。

    なぜ _generate_final_summary を直接呼ばないか (2026-07-27):
      execute_plan の成否判定を直しても、run_plan_and_execute の戻り値は
      「修正回数を使い切った」「Critic が ok と言った」経路で無条件に Gemini の
      要約になっていた。要約は失敗を説明する文章であって失敗接頭辞を持たないため、
      learning_db が outcome="failed" と記録した実行が Discord には
      ✅ Mission Complete で出る。評価ハーネスの failure_report がこれを実測した
      (T18/T19: 注入した失敗が2回とも needs_revision で終わり、for を抜けた)。

    全ステップ失敗 = 目標未達とみなしてよい理由 (Phase C1 で根拠が変わった):
      以前は「execute_plan は最初の失敗で break するので、成功0件＝どの修正計画も
      1歩目から進めなかった」と書いていた。fan-out 導入後、break は
      **ウェーブ境界** で起きるので「1歩目」は正しくない — 同時実行した3件が
      全部失敗して止まる、という形もある。
      それでも結論は変わらない: 実行されたステップが1件も成功していない以上、
      計画は最初の関門を越えていない。**理由の方だけが古くなった**ので書き換える
      (根拠が実態と食い違ったまま残っているコメントは、次に読む人を誤らせる)。

    部分失敗 (成功と失敗が混ざる) を別扱いにする理由 (2026-07-27 追記):
      全滅だけを直した時点では、3ステップ中2つ成功・保存だけ失敗のような実行が
      log_execution では outcome="partial" なのに Discord では ✅ Mission Complete
      で出ていた。「失敗を ✅ で表示しない」規約が半分だけ破れた状態。
      失敗接頭辞は付けられない (成功が混ざっているので "タスク失敗:" は嘘になる)
      ため、PARTIAL_PREFIX という第3の印を付けて mission_header に ⚠️ を出させる。
    """
    if not results:
        return _generate_final_summary(user_query, results)

    failed = [r for r in results if not r.get("success")]
    if len(failed) == len(results):
        reason = critique.get("feedback") or "全てのステップが失敗しました"
        detail = str(failed[-1].get("observation", ""))[:300]
        print("[P&E] ❌ 成功したステップが1件も無いため、失敗として報告します")
        return f"タスク失敗: {reason}" + (f"\n\n最後のエラー: {detail}" if detail else "")

    summary = _verify_summary(_generate_final_summary(user_query, results), results)
    if not failed:
        return summary

    # 失敗したステップを要約本文とは別に明示する。Gemini の要約は失敗に触れる
    # とは限らず、触れても目立たない (自己申告を最終判定に使わない)。
    steps = "、".join(f"Step {r.get('step', '?')} ({r.get('tool', '?')})"
                      for r in failed[:3])
    print(f"[P&E] ⚠️ {len(failed)}/{len(results)} ステップが失敗 → 部分完了として報告します")
    return f"{PARTIAL_PREFIX}\n失敗したステップ: {steps}\n\n{summary}"


def _verify_summary(summary: str, results: list) -> str:
    """要約の各文が観測に支持されているかを検査し、支持の無い文に印を付ける。

    ここに挟む理由 (Phase D2):
      _final_report は /agent・/chat agent・main.py CLI の3経路が合流する唯一の
      地点。入口ごとに書くと「/agent だけ直して /chat agent を取り残す」という
      このプロジェクトの既知の失敗をなぞる (CLAUDE.md の ✅ ハードコード事件)。

    lib.verifier を遅延 import するのは、フラグ OFF のときに requests の
    import 代を払わせないため。import 自体の失敗でも要約は必ず返す。
    """
    try:
        from lib.verifier import verify_report
        return verify_report(summary, results)
    except Exception as e:
        print(f"[P&E] ⚠️ 根拠検査を実施できませんでした: {e}")
        return summary


def _build_capability_block() -> str:
    """このシステムが実際に持つ機能の一覧を、ツールレジストリから組み立てる。

    ハードコードした一覧を持たない理由:
      ツールを足したときに更新を忘れた一覧は、「持っている機能を持っていないと言う」
      新種の嘘になる。レジストリを唯一の真実に保つ。
    usage を落として name + description だけにするのは、これが毎回の要約プロンプトに
    載るため (機能の有無を判断するのに引数シグネチャは要らない)。
    """
    config_tools, _ = _get_tools_config()
    return "\n".join(f"- {t['name']}: {t['description']}" for t in config_tools)


def _build_final_summary_prompt(user_query: str, results: list) -> str:
    """最終サマリー生成用のプロンプトを組み立てる (Gemini は呼ばない)。

    _generate_final_summary から切り出してある理由 (2026-07-28):
      プロンプトの中身が API 呼び出しの中に埋まっていると、規則が入っているか
      どうかをテストで確かめられない。純関数にして tests/test_answer_grounding.py
      から検証できるようにする。
    """
    results_text = "\n".join(
        f"Step {r['step']}: {r['task']} → {'✅' if r['success'] else '❌'} {str(r['observation'])[:1500]}"
        for r in results
    )

    return f"""以下のタスク実行結果を、ユーザーへの報告にまとめてください。

【ユーザーの要求】: {user_query}

【実行結果】:
{results_text}

【出力フォーマット】
以下の2部構成で出力してください：

## 📋 結論
- 実行結果の要点を2〜3行で簡潔にまとめる
- 作成したファイルがあれば [[ファイル名]] 形式でリンクを記載

## 📝 詳細レポート
- 実行結果から得られた具体的な情報を整理して記述する
- 検索で得たデータ、数値、固有名詞、手順などを漏らさず含める
- 箇条書きや見出しを使って読みやすく構造化する
- 「調べて」系の要求の場合は、調べた内容の本文をここに充実させること

【注意】
- 実行結果の observation に含まれる具体的な情報は可能な限り詳細レポートに反映する
- 一般論や抽象的な表現だけで終わらせず、実データに基づいた内容にする

【出典の扱い】（重要！）
- observation に `- File: [[ファイル名#見出し]]` の形で出典が付いている場合、
  その内容を使った文の直後に **リンクをそのままの表記でコピーして** 添えること。
  `#見出し` を省略・要約・整形してはならない（Obsidian は見出しの完全一致で
  ジャンプするため、1文字変えるとリンクが死ぬ）
- **observation に出てこないリンクを書いてはならない**。それらしいファイル名を
  推測して [[...]] を作るのは捏造であり、出典を付ける意味を失わせる
- Vault検索の結果を1件も使わなかった場合は、出典を書かずに
  「過去ノートには該当する記述が見つかりませんでした」と明示する

【時期整合性の確認】（重要！）
- ユーザーの要求が「今日」「本日」「急に」のように特定の時期の出来事を尋ねている
  場合、引用する事実がいつ発表・適用されたものかを本文中に明示すること
- observation 中の事実の時期が、ユーザーが尋ねている時期と食い違う場合
  （例: 「今日」の値動きを尋ねているのに検索結果が数ヶ月前の制度変更の話）、
  それを直接の理由として断定してはならない。時期が食い違うことを明示した上で、
  「直接の原因かは検索結果からは確認できない」といった留保を付けること
- 事実そのものが observation にあるかどうかだけでなく、その事実が
  ユーザーの質問の時間軸に対して妥当かどうかも自分で検証すること

【このシステムがエージェント実行で使えるツールの一覧】（エージェントが呼べるのはこれで全て）
{_build_capability_block()}

【自己言及の要求に答えるときの規則】（重要！）
要求が「このAI」「このエージェント」「あなた」など、このシステム自身の機能・使い方・
仕様についてのものである場合:
- 上記の一覧だけが根拠になる。Web検索で得た他社製品や一般記事の説明は、
  このシステムがその機能を持つ証拠には一切ならない
- 一覧に該当する機能が無ければ「その機能はエージェントのツールとしては存在しません」と
  明言する。検索結果の内容を、このシステムの機能・仕様・料金プラン・対応プランとして
  記述してはならない
- 近い機能があれば代替として案内してよいが、「要求された機能とは別物である」と
  明示すること
- ただし上の一覧は「エージェントが呼べるツール」であって、このシステムの全機能では
  ない。これとは別に Discord のスラッシュコマンド (約27種) と定時ジョブがあり、
  それらはこの一覧に含まれない。**ツール一覧に無いというだけで「この機能は
  存在しない」と結論してはならない**。ツールとして無いことだけを述べ、
  「Discordコマンド側にある可能性があるので確認してほしい」と添えること"""


def _generate_final_summary(user_query: str, results: list) -> str:
    """Gemini を使って実行結果から最終サマリーを生成する。

    プロンプト組み立ても try の中に入れている理由:
      この関数は「何があっても文字列を返す」契約 (失敗時は _summarize_results)。
      _build_final_summary_prompt は _get_tools_config() を呼ぶので、以前の
      純粋な文字列連結と違って例外を投げうる。try の外に出すと要約が無言で
      消える経路ができる。
    """
    try:
        prompt = _build_final_summary_prompt(user_query, results)
        response = _gemini_call(MODEL_NAME_HEAVY, prompt)
        return response.text.strip()
    except Exception:
        return _summarize_results(results)


# ---------------------------------------------------------
# Self-Improvement Protocol
# ---------------------------------------------------------

# 学習DBに保持する最大エントリ数。超過分は archive JSONL へ退避する。
LEARNING_DB_MAX_ENTRIES = 200
# 信頼性・失敗パターン集計で参照する直近エントリ数（古い烙印を残さないための窓）
RELIABILITY_WINDOW = 30


def _load_learning_db() -> dict:
    """学習DBを読み込む。存在しなければ空のDBを返す。

    ファイルは存在するのにパース失敗した場合（=破損）は、default で黙って
    握り潰すと次の1回の書き込みで全履歴が恒久消滅する。そのため破損ファイルは
    .corrupt-<timestamp> へ退避してから空DBで再開する。「無い」と「壊れている」を
    区別する責務はこの呼び出し側にあり、汎用の load_json は変更しない。
    """
    if not os.path.exists(LEARNING_DB_PATH):
        return {"executions": []}
    try:
        with open(LEARNING_DB_PATH, 'r', encoding='utf-8') as f:
            db = json.load(f)
        if not isinstance(db, dict) or "executions" not in db:
            raise ValueError("learning_db に 'executions' キーが無い")
        return db
    except (json.JSONDecodeError, ValueError, IOError) as e:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        quarantine = f"{LEARNING_DB_PATH}.corrupt-{stamp}"
        try:
            os.replace(LEARNING_DB_PATH, quarantine)
            print(f"[P&E] ⚠️ learning_db が破損 ({e}) → {quarantine} へ退避し空DBで再開")
        except OSError as move_err:
            print(f"[P&E] ⚠️ learning_db が破損 ({e}) だが退避に失敗: {move_err}")
        return {"executions": []}


def _save_learning_db(db: dict) -> None:
    """学習DBを保存する。"""
    save_json(LEARNING_DB_PATH, db)


def _next_exec_id(db: dict) -> str:
    """単調増加カウンタ db['seq'] から exec_id を採番する。

    旧実装は len(executions)+1 を採番の実体にしていたため、ローテーションで
    リストを切り詰めると採番が巻き戻り同一日にIDが二度発行されうる。seq を
    リスト長から切り離す。seq 未設定の既存DBは len(executions) から始めるので、
    移行直後の次IDは旧実装の出力と一致する。
    """
    seq = db.get("seq", len(db.get("executions", [])))
    seq += 1
    db["seq"] = seq
    return f"exec_{datetime.datetime.now().strftime('%Y%m%d')}_{seq:03d}"


def _rotate_executions(db: dict, max_entries: int):
    """(kept, overflow) を返す純粋関数。overflow は古い方から溢れたエントリ。

    db自体は書き換えない。max_entries 以下ならoverflowは空。
    """
    execs = db.get("executions", [])
    if len(execs) <= max_entries:
        return execs, []
    overflow = execs[:-max_entries]
    kept = execs[-max_entries:]
    return kept, overflow


def _append_archive(entries: list) -> None:
    """溢れたエントリを archive JSONL に1行1エントリで追記する。"""
    os.makedirs(os.path.dirname(LEARNING_DB_ARCHIVE_PATH) or '.', exist_ok=True)
    with open(LEARNING_DB_ARCHIVE_PATH, 'a', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_execution(query: str, plan: list, results: list,
                  critique: dict, duration_seconds: float,
                  parallel: dict = None) -> None:
    """実行結果を学習DBに記録する。

    parallel は fan-out (Phase C1) の実測 {"waves": n, "saved_seconds": x}。
    **追加のみ** で、既存エントリを読む側 (extract_patterns / eval) は
    .get() で参照するため過去分は無影響。省略時は記録しない
    (「0秒短縮」と「そもそも測っていない」を区別できるようにする)。
    """
    db = _load_learning_db()

    all_success = all(r.get("success", False) for r in results)
    any_success = any(r.get("success", False) for r in results)
    if all_success:
        outcome = "success"
    elif any_success:
        outcome = "partial"
    else:
        outcome = "failed"

    failure_reason = None
    failed_step = None
    for r in results:
        if not r.get("success", False):
            failed_step = r.get("step")
            obs = str(r.get("observation", ""))
            failure_reason = obs[:300]
            break

    tools_used = list(dict.fromkeys(r.get("tool", "") for r in results))

    now = datetime.datetime.now()
    exec_id = _next_exec_id(db)

    entry = {
        "id": exec_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "query": query[:200],
        "plan_steps": len(plan),
        "outcome": outcome,
        "failure_reason": failure_reason,
        "failed_step": failed_step,
        "steps_count": len(results),
        "duration_seconds": round(duration_seconds, 1),
        "tools_used": tools_used,
        "critique_status": critique.get("status", "unknown"),
    }
    if parallel and parallel.get("waves"):
        entry["parallel"] = {
            "waves": parallel.get("waves", 0),
            "saved_seconds": round(float(parallel.get("saved_seconds", 0.0)), 2),
        }

    db["executions"].append(entry)

    # ローテーション: 溢れた分を archive へ退避してから初めて trim する。
    # archive 書き込みに失敗したら trim しない（捨てるより太らせる方が安全）。
    kept, overflow = _rotate_executions(db, LEARNING_DB_MAX_ENTRIES)
    if overflow:
        try:
            _append_archive(overflow)
            db["executions"] = kept
        except Exception as e:
            print(f"[P&E] ⚠️ archive 追記に失敗 ({e}) → trim をスキップし全件保持")

    _save_learning_db(db)
    print(f"[P&E] 📝 Logged execution: {exec_id} ({outcome})")


def extract_patterns() -> dict:
    """学習DBから成功/失敗パターンとツール信頼性を抽出する。

    Returns:
        {
            "total": int,
            "success_rate": float,
            "avg_duration": float,
            "tool_reliability": {tool_name: rate, ...},
            "failure_patterns": ["..."],
            "insights": "Gemini による自然言語サマリー"
        }
    """
    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) < 5:
        return {"total": len(execs), "message": "データ不足（5件未満）"}

    total = len(execs)
    success_count = sum(1 for e in execs if e["outcome"] == "success")
    success_rate = success_count / total if total > 0 else 0
    avg_duration = sum(e.get("duration_seconds", 0) for e in execs) / total

    # ツール別信頼性
    tool_calls = {}
    tool_success = {}
    for e in execs:
        is_ok = e["outcome"] in ("success", "partial")
        for t in e.get("tools_used", []):
            if not t:
                continue
            tool_calls[t] = tool_calls.get(t, 0) + 1
            if is_ok:
                tool_success[t] = tool_success.get(t, 0) + 1

    tool_reliability = {}
    for t, count in tool_calls.items():
        tool_reliability[t] = round(tool_success.get(t, 0) / count, 2)

    # 失敗パターン
    failure_patterns = []
    for e in execs:
        if e["outcome"] == "failed" and e.get("failure_reason"):
            failure_patterns.append(
                f"Query: {e['query'][:60]} → {e['failure_reason'][:100]}"
            )

    # Gemini でパターン要約（データが十分な場合のみ）
    insights = ""
    if len(execs) >= 10:
        try:
            summary_data = json.dumps({
                "total": total,
                "success_rate": round(success_rate, 2),
                "avg_duration": round(avg_duration, 1),
                "tool_reliability": tool_reliability,
                "recent_failures": failure_patterns[-5:],
            }, ensure_ascii=False)

            prompt = f"""以下のAIエージェント実行統計を分析し、改善ポイントを3つ箇条書きで述べてください。

{summary_data}

簡潔に（各項目1-2文）:"""

            resp = _gemini_call(MODEL_NAME, prompt)
            insights = resp.text.strip()
        except Exception:
            insights = ""

    return {
        "total": total,
        "success_rate": round(success_rate, 2),
        "avg_duration": round(avg_duration, 1),
        "tool_reliability": tool_reliability,
        "failure_patterns": failure_patterns[-10:],
        "insights": insights,
    }


def get_enhanced_prompt(user_query: str) -> str:
    """過去の学習から関連情報を取得し、Planner プロンプトに追記する文字列を返す。"""
    parts = []
    
    # 顧客評価・自己改善ループからの学習パッチ注入
    try:
        from engine_self_improve import get_approved_patch_hints
        patches = get_approved_patch_hints()
        if patches:
            parts.append(patches)
    except Exception as e:
        print(f"[SelfImprove] Error loading patches: {e}")

    db = _load_learning_db()
    execs = db.get("executions", [])

    if len(execs) >= 5:
        # 直近の失敗パターンを収集
        recent_failures = []
        for e in reversed(execs[-20:]):
            if e["outcome"] == "failed" and e.get("failure_reason"):
                recent_failures.append(
                    f"- クエリ「{e['query'][:50]}」→ {e['failure_reason'][:100]}"
                )
            if len(recent_failures) >= 3:
                break

        # ツール信頼性の低いものを警告（直近 RELIABILITY_WINDOW 件のみ集計）。
        # 全件走査だと一度 rate<0.8 に落ちたツールが以後どれだけ成功しても
        # 全期間平均が上がりきらず「要注意」の烙印が残り続けるため窓を切る。
        recent_execs = execs[-RELIABILITY_WINDOW:]
        tool_calls = {}
        tool_success = {}
        for e in recent_execs:
            is_ok = e["outcome"] in ("success", "partial")
            for t in e.get("tools_used", []):
                if not t:
                    continue
                tool_calls[t] = tool_calls.get(t, 0) + 1
                if is_ok:
                    tool_success[t] = tool_success.get(t, 0) + 1

        low_reliability = []
        for t, count in tool_calls.items():
            if count >= 3:
                rate = tool_success.get(t, 0) / count
                if rate < 0.8:
                    low_reliability.append(f"- {t}: 成功率 {rate:.0%}（要注意）")

        # 平均ステップ数
        avg_steps = sum(e.get("plan_steps", 0) for e in execs) / len(execs)

        if recent_failures:
            parts.append("【過去の失敗から学んだ注意事項】")
            parts.extend(recent_failures)
        if low_reliability:
            parts.append("【信頼性の低いツール】")
            parts.extend(low_reliability)
        if avg_steps:
            parts.append(f"【参考】平均的なタスクは {avg_steps:.1f} ステップで完了している")

    return "\n".join(parts) if parts else ""


def generate_performance_dashboard() -> str:
    """Performance Dashboard を Obsidian に出力する。"""
    patterns = extract_patterns()

    if patterns.get("message"):
        return f"データ不足: {patterns['message']}"

    total = patterns["total"]
    rate = patterns["success_rate"]
    avg_dur = patterns["avg_duration"]
    tool_rel = patterns.get("tool_reliability", {})
    failures = patterns.get("failure_patterns", [])
    insights = patterns.get("insights", "")

    # ツール信頼性ランキング
    sorted_tools = sorted(tool_rel.items(), key=lambda x: x[1], reverse=True)
    tool_lines = "\n".join(
        f"| {t} | {r:.0%} |" for t, r in sorted_tools
    )

    # 失敗パターン
    fail_lines = "\n".join(f"- {f}" for f in failures[-5:]) if failures else "なし"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    dashboard = f"""# Agent Performance Dashboard
> Updated: {now}

## 全体統計
| 指標 | 値 |
|---|---|
| 総実行回数 | {total} |
| 成功率 | {rate:.0%} |
| 平均実行時間 | {avg_dur:.1f}秒 |

## ツール信頼性
| ツール | 成功率 |
|---|---|
{tool_lines}

## 最近の失敗パターン
{fail_lines}

## AI分析
{insights if insights else '_データ蓄積中（10件以上で分析開始）_'}
"""

    with open(PERFORMANCE_DASHBOARD_PATH, 'w', encoding='utf-8') as f:
        f.write(dashboard)

    return f"Dashboard updated: {PERFORMANCE_DASHBOARD_PATH}"

