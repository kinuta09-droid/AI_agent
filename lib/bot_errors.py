# -*- coding: utf-8 -*-
"""Discord コマンド層で捕まえた例外を「必ずログに残す」ための共通ヘルパー。

なぜ必要か:
  2026-02-28 のリファクタで /curate が NameError のまま5ヶ月放置された。
  壊れていたこと自体より問題だったのは、`except Exception as e:` が
  `Error: {e}` を Discord に送るだけで **ログに一行も残さなかった** こと。
  discord.py は未捕捉のコマンド/View 例外を自前のロガーで stderr に出すため、
  手書きの except は例外処理を足したつもりで可観測性を下げていた。

出力先:
  print() の行き先は bot プロセスの stdout。main.py が Popen で継承させ、
  scripts/arms_core.bat が logs/launcher.log にリダイレクトしている。
  bot_discord.py:39 で stdout を utf-8 に reconfigure 済みなので、
  日本語を含む例外メッセージを print しても cp932 で落ちない
  (CLAUDE.md の Encoding Safety Rules LEVEL 0 に沿う。環境変数は触らない)。
"""
import traceback

# Discord に返す例外メッセージの上限。全文は必ずログ側に出るので、
# ここで切っても情報は失われない。Gemini/ccxt が数KBの JSON を
# 例外メッセージに載せてくることがあり、それをそのまま Discord に
# 流すと本題が読めなくなるため。
MAX_USER_MESSAGE = 500

# 「文字列で失敗を返す」規約の接頭辞。判定を各コマンドで手書きすると必ず割れる
# (log_command_error() の戻り値を "Error (型名): ..." に変えたとき、"Error:" 固定で
#  書かれていた /agent の判定だけが取り残されかけた)。追加する側が
#  ここを見に来られるよう、判定の起点は必ずこの定数にする。
#   - "Error:"  … tools/ の `return f"Error: {e}"` および engine 層の戻り値契約
#   - "Error (" … log_command_error() が型名を挟む形式
# "Error" と緩めないのは、「Errorハンドリングの調査結果」のような正常な本文が
# Error で始まり得るため。これらは前方一致で使うので tuple のまま公開する。
FAILURE_PREFIXES = ("Error:", "Error (")


def is_failure_result(result, extra_prefixes: tuple = ()) -> bool:
    """ツール/エンジンが文字列で返した結果が失敗かどうかを判定する。

    成功と失敗を同じ ✅ で表示してしまう事故 (2026-07-26) を防ぐための単一窓口。

    Args:
        result: 判定対象。str 以外は「失敗ではない」とみなす。
        extra_prefixes: 呼び出し側固有の失敗接頭辞 (例: "タスク失敗:")。
    """
    if not isinstance(result, str):
        return False
    return result.startswith(FAILURE_PREFIXES + tuple(extra_prefixes))


def log_command_error(where: str, exc: BaseException,
                      user_prefix: str = "Error") -> str:
    """例外をスタックトレース付きでログに残し、Discord 表示用の文字列を返す。

    except 節の中から呼ぶこと (traceback.format_exc() が現在の例外を読むため)。

    Args:
        where: 発生箇所の識別子。スラッシュコマンド名を推奨 ("/curate" 等)。
        exc: 捕捉した例外。
        user_prefix: 戻り値の先頭に置く文言。呼び出し側の既存の表記
            ("❌ XTrend Error" 等) を保つために使う。

    Returns:
        f"{user_prefix} ({例外の型名}): {メッセージ}" — 型名を入れるのは、
        str(exc) が空になる例外 (KeyError や独自例外) で「何も分からない
        Error: だけの行」が出るのを防ぐため。
    """
    exc_type = type(exc).__name__
    print(f"[Discord] ❌ {where} failed: {exc_type}: {exc}")
    # format_exc() は末尾に改行を含むので end="" にする。launcher.log は
    # 起動失敗時に真っ先に読むログなので、余計な空行を挟まない。
    print(traceback.format_exc(), end="")

    message = str(exc)
    if len(message) > MAX_USER_MESSAGE:
        message = message[:MAX_USER_MESSAGE] + "… (全文は logs/launcher.log)"
    return f"{user_prefix} ({exc_type}): {message}"
