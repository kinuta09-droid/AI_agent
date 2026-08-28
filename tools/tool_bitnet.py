# -*- coding: utf-8 -*-
"""
tool_bitnet.py — BitNet b1.58 Local Inference Tool
- ローカルルーター  : Gemini API不要でツール選択
- 要約             : Obsidianノートや長文をCPUでオフライン要約
- 下書き生成       : Discordメッセージ等の下書きをローカル生成
- コード生成       : cron式・正規表現・Pythonスニペットを生成
- Discordフィルター: 挨拶/スパムを即判定、Geminiフォールバック
- バッチタグ付け   : Obsidianノートに英語タグを一括付与
"""
import os
import re
import subprocess
import time
import tempfile
import logging

# BitNet インストールパス (実体は lib/paths.py。.env の BITNET_DIR で差し替え可)
from lib.paths import BITNET_DIR

LLAMA_CLI = os.path.join(BITNET_DIR, "build", "bin", "Release", "llama-cli.exe")
MODEL_PATH = os.path.join(BITNET_DIR, "models", "BitNet-b1.58-2B-4T", "ggml-model-i2_s.gguf")
THREADS = 4  # i7-13620H の最適値

logger = logging.getLogger(__name__)


_MARKER = "\n\n### Response:\n"

def _run_bitnet(prompt: str, max_tokens: int = 150, temperature: float = 0.3) -> dict:
    """
    llama-cli.exe を Python subprocess で直接呼び出す。
    - プロンプトをUTF-8ファイル経由で渡す → Windows cp932 クラッシュ回避
    - プロンプト末尾にマーカーを付ける → 生成テキストを確実に抽出

    Returns:
        {"text": str, "elapsed_sec": float, "tokens_per_sec": float}
    """
    if not os.path.exists(LLAMA_CLI):
        raise FileNotFoundError(f"llama-cli.exe not found: {LLAMA_CLI}")
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Model not found: {MODEL_PATH}")

    # マーカー付きプロンプトをUTF-8 tmpファイルに書く
    prompt_with_marker = prompt + _MARKER
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', suffix='.txt', delete=False
    ) as f:
        f.write(prompt_with_marker)
        prompt_file = f.name

    try:
        cmd = [
            LLAMA_CLI,
            "-m", MODEL_PATH,
            "-f", prompt_file,
            "-n", str(max_tokens),
            "-t", str(THREADS),
            "-c", "2048",
            "--temp", str(temperature),
            "-b", "1",
            "-ngl", "0",
            # NOTE: --log-disable は stdout も消してしまうため使用しない
            # stderr (モデルロードログ) は capture_output=True で吸収する
        ]

        t0 = time.time()
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            cwd=BITNET_DIR,
        )
        elapsed = time.time() - t0

        if result.returncode != 0:
            logger.warning(f"BitNet stderr: {result.stderr[-300:]}")
            raise RuntimeError(f"llama-cli exited with code {result.returncode}")

        # マーカーで生成テキストを切り出す
        # モデルが "### Response: \n" のようにスペースを挿入するケースも考慮
        output = result.stdout
        marker_keyword = "### Response:"
        if marker_keyword in output:
            after_marker = output.split(marker_keyword, 1)[1]
            # マーカー直後の空白・改行をスキップして本文を取り出す
            response = after_marker.lstrip(" \n").strip()
        elif prompt in output:
            response = output[output.index(prompt) + len(prompt):].strip()
        else:
            response = output.strip()

        # 速度推定（単語数ベース）
        token_count = max(1, len(response.split()))
        tps = token_count / max(elapsed, 0.001)

        return {
            "text": response,
            "elapsed_sec": round(elapsed, 2),
            "tokens_per_sec": round(tps, 1),
        }

    finally:
        os.unlink(prompt_file)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def route_tool(user_input: str, tool_names: list[str], tool_descriptions: dict = None) -> str:
    """
    ローカルルーター: ユーザー入力から最適ツール名を返す。
    Gemini API を呼ばないため、オフライン・低コスト・高速。

    Args:
        user_input: ユーザーの自然言語入力
        tool_names: 選択肢のツール名リスト
        tool_descriptions: {name: description} の辞書（任意）

    Returns:
        最も適したツール名 (文字列)
    """
    # ツールリストを説明付きで組み立てる（説明があれば精度向上）
    if tool_descriptions:
        tool_lines = "\n".join(
            f"- {name}: {tool_descriptions.get(name, '')}"
            for name in tool_names
        )
        tool_section = f"Tools:\n{tool_lines}"
    else:
        tool_section = f"Tools: [{', '.join(tool_names)}]"

    prompt = (
        f"You are a task router. Select the single best tool for the user's request.\n"
        f"{tool_section}\n\n"
        f"Rules: Reply with ONLY the tool name. No explanation.\n"
        f"User request: \"{user_input}\"\n"
        f"Best tool name:"
    )
    try:
        t0 = time.time()
        result = _run_bitnet(prompt, max_tokens=10, temperature=0.1)
        elapsed = time.time() - t0

        # レスポンスからツール名を抽出（前後の空白・改行を除去）
        raw = result["text"].strip().split()[0].rstrip(".,;:")
        # 最も近いツール名にマッチング
        matched = next((t for t in tool_names if t.lower() in raw.lower()), None)
        if not matched:
            matched = next((t for t in tool_names if raw.lower() in t.lower()), tool_names[-1])

        logger.info(f"[BitNet Router] '{user_input[:30]}' → {matched} ({elapsed:.1f}s)")
        return matched

    except Exception as e:
        logger.error(f"[BitNet Router] fallback due to error: {e}")
        return tool_names[-1]  # fallback: 最後のツール（通常 "agent"）


def route_tool_confident(
    user_input: str,
    tool_names: list[str],
    tool_descriptions: dict = None,
    catchall: str = "agent",
) -> tuple[str | None, str]:
    """
    確信度付きローカルルーター（二段ルーティングの一次判定用）。

    既存 route_tool() は必ずツール名を返す（曖昧時は tool_names[-1] にフォールバック）
    が、この関数は「BitNetがツール名を *そのまま* 出力した高確信ケース」だけツール名を返し、
    曖昧・キャッチオール・空出力・例外時は None を返す。呼び出し側（main.route_to_tool）が
    None を見て Gemini へエスカレーションできるようにするための確信度シグナル版。

    高確信の定義: BitNet出力の先頭トークンに実ツール名が forward 一致すること
    （t.lower() in raw.lower()）。reverse/曖昧一致は precision 優先で不採用にし、
    誤ルーティングによるサイレント劣化の被害範囲を最小化する。

    Args:
        user_input: ユーザーの自然言語入力
        tool_names: 選択肢のツール名リスト
        tool_descriptions: {name: description} の辞書（任意・精度向上）
        catchall: 汎用フォールバックツール名。これに一致してもエスカレーション扱い

    Returns:
        (tool_name, reason):
          - (str, "clean_match"): 高確信。Gemini をスキップしてよい
          - (None, reason):       低確信。呼び出し側で Gemini へフォールバック
                                  reason ∈ {"no_tools","empty_output","catchall",
                                            "ambiguous","error:<msg>"}
    """
    if not tool_names:
        return None, "no_tools"

    # ツールリストを説明付きで組み立てる（route_tool と同じ書式）
    if tool_descriptions:
        tool_lines = "\n".join(
            f"- {name}: {tool_descriptions.get(name, '')}"
            for name in tool_names
        )
        tool_section = f"Tools:\n{tool_lines}"
    else:
        tool_section = f"Tools: [{', '.join(tool_names)}]"

    prompt = (
        f"You are a task router. Select the single best tool for the user's request.\n"
        f"{tool_section}\n\n"
        f"Rules: Reply with ONLY the tool name. No explanation.\n"
        f"User request: \"{user_input}\"\n"
        f"Best tool name:"
    )

    try:
        result = _run_bitnet(prompt, max_tokens=10, temperature=0.1)
    except Exception as e:
        # exe不在・subprocess失敗は縮退: 呼び出し側で Gemini へ委譲させる
        logger.error(f"[BitNet ConfidentRouter] escalate (exec error): {e}")
        return None, f"error:{e}"

    raw_text = (result.get("text") or "").strip()
    if not raw_text:
        logger.info("[BitNet ConfidentRouter] escalate (empty output)")
        return None, "empty_output"

    raw = raw_text.split()[0].rstrip(".,;:")

    # 高確信: ツール名が出力先頭トークンに forward 一致（かつ catchall ではない）
    clean = next((t for t in tool_names if t.lower() in raw.lower()), None)
    if clean and clean != catchall:
        logger.info(
            f"[BitNet ConfidentRouter] '{user_input[:30]}' → {clean} "
            f"({result.get('elapsed_sec')}s, clean_match)"
        )
        return clean, "clean_match"

    # 曖昧一致 / catchall一致 / 無一致 は Gemini へ委譲
    reason = "catchall" if clean == catchall else "ambiguous"
    logger.info(
        f"[BitNet ConfidentRouter] '{user_input[:30]}' → escalate "
        f"({result.get('elapsed_sec')}s, {reason}, raw={raw!r})"
    )
    return None, reason


def summarize(text: str, lines: int = 3) -> str:
    """
    テキストをローカルで要約する。Obsidianノートや長文に使用。

    Args:
        text: 要約対象テキスト
        lines: 要約の行数（デフォルト3行）

    Returns:
        要約テキスト
    """
    prompt = (
        f"以下のテキストを{lines}行以内で簡潔に日本語で要約してください。\n\n"
        f"テキスト:\n{text[:2000]}\n\n"
        f"要約:"
    )
    try:
        result = _run_bitnet(prompt, max_tokens=200, temperature=0.3)
        summary = result["text"].strip()
        logger.info(f"[BitNet Summarize] {result['elapsed_sec']}s, ~{result['tokens_per_sec']} t/s")
        return summary
    except Exception as e:
        logger.error(f"[BitNet Summarize] error: {e}")
        return f"[要約失敗: {e}]"


def draft_message(context: str, style: str = "discord") -> str:
    """
    メッセージの下書きをローカル生成する。

    Args:
        context: メッセージの内容・意図
        style: "discord" | "email" | "note"

    Returns:
        下書きテキスト
    """
    style_guide = {
        "discord": "短く親しみやすいDiscordアナウンスメント（2〜3文）",
        "email":   "丁寧なビジネスメール本文（3〜5文）",
        "note":    "Obsidian用のMarkdownノート見出しと要点",
    }.get(style, "自然な日本語の文章")

    prompt = (
        f"次の内容について{style_guide}を日本語で書いてください。\n\n"
        f"内容: {context}\n\n"
        f"下書き:"
    )
    try:
        result = _run_bitnet(prompt, max_tokens=150, temperature=0.7)
        draft = result["text"].strip()
        logger.info(f"[BitNet Draft] {result['elapsed_sec']}s, style={style}")
        return draft
    except Exception as e:
        logger.error(f"[BitNet Draft] error: {e}")
        return f"[下書き失敗: {e}]"


# ─────────────────────────────────────────────
# 1. cron / regex / Python スニペット生成
# ─────────────────────────────────────────────

# pattern_type ごとのプロンプトテンプレート
_PATTERN_PROMPTS = {
    "cron": (
        "Generate a cron expression for the following schedule.\n"
        "Reply with ONLY the cron expression (5 fields: min hour dom mon dow). No explanation.\n"
        "Schedule: \"{desc}\"\n"
        "Cron:"
    ),
    "regex": (
        "Generate a Python regex pattern for the following requirement.\n"
        "Reply with ONLY the raw pattern string (no r'' wrapper, no explanation).\n"
        "Requirement: \"{desc}\"\n"
        "Pattern:"
    ),
    "python": (
        "Write a concise Python code snippet for the following task.\n"
        "Reply with ONLY runnable Python code, no markdown fences.\n"
        "Task: \"{desc}\"\n"
        "Code:"
    ),
}

def generate_pattern(description: str, pattern_type: str = "regex") -> str:
    """
    cron式・正規表現・Pythonスニペットをローカルで生成する。

    Args:
        description: 自然言語での要件（英語推奨）
        pattern_type: "cron" | "regex" | "python"

    Returns:
        生成されたパターン文字列
    """
    template = _PATTERN_PROMPTS.get(pattern_type, _PATTERN_PROMPTS["python"])
    prompt = template.format(desc=description)
    max_tok = {"cron": 20, "regex": 40, "python": 80}.get(pattern_type, 80)

    try:
        result = _run_bitnet(prompt, max_tokens=max_tok, temperature=0.1)
        raw = result["text"].strip()

        if pattern_type == "cron":
            # 5フィールドのcron式を正規表現で抽出（余分なテキストを除去）
            m = re.search(r'([-*0-9,/]+\s+[-*0-9,/]+\s+[-*0-9,/]+\s+[-*0-9,/]+\s+[-*0-9,/]+)', raw)
            raw = m.group(1).strip() if m else raw.splitlines()[0].strip()

        elif pattern_type == "regex":
            # markdownフェンス除去 (re.MULTILINEなし: string先頭のみ)
            raw = re.sub(r'^```\w*\s*', '', raw).strip()
            raw = re.sub(r'\s*```.*$', '', raw, flags=re.DOTALL).strip()
            # 1行目のみ取得
            raw = raw.splitlines()[0].strip()
            # r"..." ラッパー除去
            m = re.match(r'^r["\'](.+?)["\']', raw)
            if m:
                raw = m.group(1)
            # 末尾の既知ジャンクワード除去（例: "Response", "Example"）
            # \w+$ の一律除去はregex末尾のパターンを破壊するため使用しない
            raw = re.sub(r'\s+(Response|Example|Output|Answer|Result)$', '', raw, flags=re.IGNORECASE)

        elif pattern_type == "python":
            # markdownコードフェンス除去して中身を返す
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```.*$', '', raw, flags=re.DOTALL)
            raw = raw.strip()

        logger.info(f"[BitNet Pattern] type={pattern_type}, result={raw!r} ({result['elapsed_sec']}s)")
        return raw
    except Exception as e:
        logger.error(f"[BitNet Pattern] error: {e}")
        return f"[生成失敗: {e}]"


# ─────────────────────────────────────────────
# 2. Discord メッセージ フィルター
# ─────────────────────────────────────────────

# キーワードで即判定できる挨拶・スパムパターン（BitNet不要）
_GREETING_PATTERNS = re.compile(
    r"^(こんにちは|こんばんは|おはよう|おやすみ|ありがとう|よろしく|はじめまして"
    r"|hi|hello|hey|good\s*(morning|evening|night)|thanks|thank\s*you"
    r"|[wｗ]{2,}|草|ｗｗ|笑|lol|lmao)\W*$",
    re.IGNORECASE,
)
_SPAM_PATTERNS = re.compile(
    r"^(test|てすと|テスト|\.{3,}|！{2,}|[。、]{2,})\s*$",
    re.IGNORECASE,
)

# キーワード判定の返答セット（Gemini不要）
_GREETING_REPLIES = {
    "こんにちは": "こんにちは！何かお手伝いできることはありますか？",
    "こんばんは": "こんばんは！今日はどんなことをお手伝いしましょうか？",
    "おはよう": "おはようございます！今日もよろしくお願いします。",
    "おやすみ": "おやすみなさい！",
    "ありがとう": "どういたしまして！",
    "よろしく": "こちらこそ、よろしくお願いします！",
    "はじめまして": "はじめまして！何かお手伝いできることがあればお気軽にどうぞ。",
    "hi": "Hi there! How can I help you?",
    "hello": "Hello! What can I do for you?",
    "hey": "Hey! What can I help you with?",
    "thanks": "You're welcome!",
    "thank you": "You're welcome! Happy to help.",
}

def quick_filter_discord(message: str) -> dict:
    """
    Discordメッセージを即時フィルタリングする（BitNet不使用、キーワードベース）。

    挨拶・スパムを検出してGemini API呼び出しをスキップする。
    DiscordBot の on_message で Gemini 呼び出し前に使う。

    Returns:
        {
          "action": "reply" | "skip" | "pass",
          "reply": str | None,   # action=="reply" のとき返答文
          "reason": str
        }
    """
    text = message.strip()
    if not text:
        return {"action": "skip", "reply": None, "reason": "empty"}

    # スパム・ノイズ判定
    if _SPAM_PATTERNS.match(text):
        return {"action": "skip", "reply": None, "reason": "spam/noise"}

    # 挨拶判定: まず固定返答を探し、なければ汎用返答
    if _GREETING_PATTERNS.match(text):
        # マッチしたキーワードで返答を選ぶ
        reply = next(
            (v for k, v in _GREETING_REPLIES.items() if k in text.lower()),
            "こんにちは！何かお手伝いできますか？"
        )
        return {"action": "reply", "reply": reply, "reason": "greeting"}

    # 上記以外はGeminiへパス
    return {"action": "pass", "reply": None, "reason": "needs_gemini"}


def gemini_fallback_response(message: str) -> str:
    """
    Gemini API が利用不可のときに BitNet でフォールバック応答を生成する。

    Returns:
        英語の応答テキスト（品質はGeminiより低い）
    """
    prompt = (
        "You are a helpful assistant. Reply concisely (2-3 sentences).\n"
        f"User: {message}\n"
        "Assistant:"
    )
    try:
        result = _run_bitnet(prompt, max_tokens=80, temperature=0.7)
        response = result["text"].strip().split("\n")[0]
        logger.info(f"[BitNet Fallback] {result['elapsed_sec']}s")
        return f"[オフライン応答] {response}"
    except Exception as e:
        return "[BitNet] 現在AIサービスが利用できません。しばらくお待ちください。"


# ─────────────────────────────────────────────
# 3. Obsidian ノート バッチタグ付け
# ─────────────────────────────────────────────

def generate_note_tags(title: str, content_preview: str = "", max_tags: int = 5) -> list[str]:
    """
    ノートのタイトル・冒頭テキストから英語タグを生成する。

    Args:
        title: ノートのタイトル
        content_preview: ノート冒頭200文字程度
        max_tags: 最大タグ数

    Returns:
        英語タグのリスト（例: ["crypto", "bitcoin", "analysis"]）
    """
    context = f'Title: "{title}"'
    if content_preview:
        context += f"\nPreview: {content_preview[:200]}"

    prompt = (
        f"Generate {max_tags} concise English topic tags for this note.\n"
        f"Reply with ONLY lowercase tags separated by commas. No #, no explanation.\n"
        f"{context}\n"
        f"Tags:"
    )
    try:
        result = _run_bitnet(prompt, max_tokens=25, temperature=0.2)
        raw = result["text"].strip().splitlines()[0]
        # カンマ区切りでタグを分割し、余分な記号を除去
        tags = [
            t.strip().lstrip("#").lower().replace(" ", "_")
            for t in raw.split(",")
            if t.strip() and len(t.strip()) < 30
        ][:max_tags]
        logger.info(f"[BitNet Tags] '{title[:30]}' → {tags}")
        return tags
    except Exception as e:
        logger.error(f"[BitNet Tags] error: {e}")
        return []


def batch_tag_notes(notes: list[dict]) -> list[dict]:
    """
    複数ノートに一括タグ付けする。Gemini APIコストゼロ。

    Args:
        notes: [{"title": str, "content": str}, ...] のリスト

    Returns:
        [{"title": str, "tags": list[str]}, ...] のリスト
    """
    results = []
    total = len(notes)
    for i, note in enumerate(notes, 1):
        title = note.get("title", "")
        content = note.get("content", "")
        tags = generate_note_tags(title, content[:200])
        results.append({"title": title, "tags": tags})
        logger.info(f"[BitNet BatchTag] {i}/{total}: {title[:30]} → {tags}")
    return results


def run_bitnet_interactive(goal: str = None) -> str:
    """
    main.py の TOOLS から呼び出されるエントリーポイント。
    メニュー形式でルーター/要約/下書きを選択して実行。
    """
    print("\n[BitNet b1.58 Local AI]")
    print("1: ルーティングテスト")
    print("2: テキスト要約")
    print("3: メッセージ下書き生成")
    choice = input("選択 (1-3): ").strip()

    if choice == "1":
        user_input = input("ルーティングするテキスト: ").strip()
        from main import TOOLS
        tool_names = [t["name"] for t in TOOLS]
        result = route_tool(user_input, tool_names)
        return f"[BitNet Router] → {result}"

    elif choice == "2":
        text = input("要約するテキスト (Enterで確定): ").strip()
        return summarize(text)

    elif choice == "3":
        context = input("メッセージの内容: ").strip()
        style = input("スタイル (discord/email/note) [discord]: ").strip() or "discord"
        return draft_message(context, style)

    return "[BitNet] キャンセルしました"
