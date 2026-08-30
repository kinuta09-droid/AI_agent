# -*- coding: utf-8 -*-
"""commands/bot_commands_chat.py の _parse_chat_intent の回帰テスト。

対象1: 「エージェント」等のキーワードを含まない「〇〇のサイトに行って✕✕を持ってきて」
だけの自然文でも agent_natural (Planner起動の確認ボタン) に振り分けられること。
ブラウジング系ツール (tool_browser_use/tool_browser_act) は Planner 経由でしか
呼べないため、この検知が無いとキーワード無しの発言はただの雑談応答で終わり
ツールが一切使われない (2026-08-28 追加)。

対象2: calendar 意図のトリガーから「予定」を除外したこと (2026-08-31)。
日常会話で頻出する「予定」を単体トリガーに残すと雑談でも calendar が誤起動する
ため INTENT_PATTERNS/ANYWHERE_INTENT_PATTERNS から外したが、「予定を登録して」
のような登録依頼は _CALENDAR_ADD_TRIGGER_RE (近傍語必須の複合マッチ) で
引き続き拾えることを確認する。

実行: py tests/test_chat_agent_natural.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import commands.bot_commands_chat as chat_mod

_FAIL = False


def _check(name, cond, detail=""):
    global _FAIL
    if cond:
        print(f"✅ [{name}]")
    else:
        _FAIL = True
        print(f"❌ [{name}] {detail}")


def _intent(message: str) -> str:
    intent, _ = chat_mod._parse_chat_intent(message)
    return intent


def test_url_triggers_agent_natural():
    intent = _intent("https://www.komeri.com/shop/g/g2465636/ の在庫を確認して")
    _check("URLを含む発言はagent_naturalになる", intent == "agent_natural", detail=intent)


def test_site_phrase_triggers_agent_natural():
    for msg in ["コメリのサイトに行って在庫を教えて", "そのページを開いて内容を見て", "公式サイトを見に行って"]:
        intent = _intent(msg)
        _check(f"「サイト/ページ」+行動語句はagent_naturalになる: {msg!r}", intent == "agent_natural", detail=intent)


def test_existing_agent_keywords_still_work():
    # 文頭一致すると INTENT_PATTERNS の "agent" (即実行) に先取りされるため、
    # agent_natural (文中言及→確認ボタン) を確認したい場合は文頭に置かない。
    for msg in ["この調査はエージェントに任せて", "ちょっとagentで実行してほしい", "この件planしてもらえる？"]:
        intent = _intent(msg)
        _check(f"既存キーワードの検知は壊れていない: {msg!r}", intent == "agent_natural", detail=intent)


def test_planning_word_still_excluded():
    intent = _intent("来月のplanningミーティングどうする？")
    _check("planning/planet/plant は引き続き除外される (誤検知しない)", intent != "agent_natural", detail=intent)


def test_unrelated_chat_not_affected():
    intent = _intent("今日の天気はどうですか？")
    _check("無関係な雑談はchatのまま", intent == "chat", detail=intent)


def test_restart_keyword_still_takes_priority_over_url():
    # restart は誤爆コストが最大のため他のどの判定よりも先に見る設計 (既存の優先順位)。
    # URLを含んでいても restart が優先されることを確認する。
    intent = _intent("再起動する前に https://example.com を見て")
    _check("restartはURL検知より優先される", intent == "restart", detail=intent)


def test_yotei_alone_does_not_trigger_calendar():
    for msg in ["予定", "今日は予定があって忙しい"]:
        intent = _intent(msg)
        _check(f"「予定」単体・文中ではcalendarが誤起動しない: {msg!r}", intent != "calendar", detail=intent)


def test_calendar_add_with_yotei_still_works():
    intent = _intent("明日15時に歯医者、カレンダーに予定を登録して")
    _check("「予定を登録して」は引き続きcalendar_addになる", intent == "calendar_add", detail=intent)


def test_calendar_keyword_still_works():
    intent = _intent("カレンダーを見せて")
    _check("「カレンダー」は引き続きcalendarになる", intent == "calendar", detail=intent)


def main():
    test_url_triggers_agent_natural()
    test_site_phrase_triggers_agent_natural()
    test_existing_agent_keywords_still_work()
    test_planning_word_still_excluded()
    test_unrelated_chat_not_affected()
    test_restart_keyword_still_takes_priority_over_url()
    test_yotei_alone_does_not_trigger_calendar()
    test_calendar_add_with_yotei_still_works()
    test_calendar_keyword_still_works()


if __name__ == "__main__":
    main()
    sys.exit(1 if _FAIL else 0)
