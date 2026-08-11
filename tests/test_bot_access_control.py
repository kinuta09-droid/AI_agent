# -*- coding: utf-8 -*-
"""
lib/bot_access_control.py (ALLOWED_USER_IDS ベースの認可制御) の回帰テスト

対象:
  _parse_allowed_user_ids のパース (トリム/非数字トークン無視)
  is_allowed_user の許可/拒否/未設定時フェイルオープン
  GatedCommandTree.interaction_check の許可/拒否 (ephemeral応答)

実行: py tests/test_bot_access_control.py
"""
import os
import sys
import asyncio
from unittest.mock import AsyncMock, patch

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.bot_access_control as bac


def test_parse_allowed_user_ids():
    assert bac._parse_allowed_user_ids("123, 456 ,789") == {123, 456, 789}
    assert bac._parse_allowed_user_ids("") == set()
    # 非数字トークンは警告のみで無視 (bot を落とさない)
    assert bac._parse_allowed_user_ids("123,abc,456") == {123, 456}
    print("✅ _parse_allowed_user_ids (trim/empty/non-digit ignore)")


def test_is_allowed_user():
    orig = bac.ALLOWED_USER_IDS
    try:
        bac.ALLOWED_USER_IDS = set()
        assert bac.is_allowed_user(999) is True, "未設定時はフェイルオープンで全許可"

        bac.ALLOWED_USER_IDS = {123}
        assert bac.is_allowed_user(123) is True
        assert bac.is_allowed_user(456) is False
    finally:
        bac.ALLOWED_USER_IDS = orig
    print("✅ is_allowed_user (allow/deny/fail-open)")


def _make_fake_interaction(user_id: int):
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


async def _run_gated_command_tree_check():
    orig = bac.ALLOWED_USER_IDS
    try:
        bac.ALLOWED_USER_IDS = {123}
        tree = bac.GatedCommandTree.__new__(bac.GatedCommandTree)

        ok = await tree.interaction_check(_make_fake_interaction(123))
        assert ok is True

        interaction = _make_fake_interaction(456)
        ok = await tree.interaction_check(interaction)
        assert ok is False
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("ephemeral") is True
    finally:
        bac.ALLOWED_USER_IDS = orig


def test_gated_command_tree_check():
    asyncio.run(_run_gated_command_tree_check())
    print("✅ GatedCommandTree.interaction_check (allow/deny)")


async def _run_gated_command_tree_on_error():
    from discord import app_commands

    tree = bac.GatedCommandTree.__new__(bac.GatedCommandTree)
    interaction = _make_fake_interaction(123)

    # CheckFailure (interaction_check の拒否) は正常系なので super().on_error に委譲しない
    with patch.object(app_commands.CommandTree, "on_error", new=AsyncMock()) as mock_super_on_error:
        await tree.on_error(interaction, app_commands.CheckFailure("denied"))
        mock_super_on_error.assert_not_awaited()

        # それ以外の例外は super().on_error に委譲される
        other_error = app_commands.AppCommandError("boom")
        await tree.on_error(interaction, other_error)
        mock_super_on_error.assert_awaited_once_with(interaction, other_error)


def test_gated_command_tree_on_error():
    asyncio.run(_run_gated_command_tree_on_error())
    print("✅ GatedCommandTree.on_error (CheckFailure握りつぶし/それ以外は委譲)")


if __name__ == "__main__":
    test_parse_allowed_user_ids()
    test_is_allowed_user()
    test_gated_command_tree_check()
    test_gated_command_tree_on_error()
    print("\n🎉 ALL BOT ACCESS CONTROL REGRESSION TESTS PASSED")
