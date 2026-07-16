# -*- coding: utf-8 -*-
"""
Phase② ツールレジストリ統一のユニットテスト
(get_tools_config のキャッシュ / エイリアス解決 / engine 委譲 / run_react_loop 削除)

実行: py tests/test_tool_registry.py
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import lib.tool_registry as registry
from lib.tool_registry import get_tools_config


def test_config_and_map_consistency():
    config_tools, tools_map = get_tools_config()
    assert config_tools, "config_tools must not be empty"
    for t in config_tools:
        assert set(t.keys()) == {"name", "usage", "description"}, f"unexpected keys: {t.keys()}"
    assert set(tools_map) == {t["name"] for t in config_tools}, "tools_map keys must match config names"
    assert all(callable(v) for v in tools_map.values()), "all tools_map values must be callable"
    print("✅ registry: config/map consistency & callables")


def test_cache_reuse_and_isolation():
    c1, m1 = get_tools_config()
    assert registry._CACHE is not None, "cache must be populated after first call"
    c1.append({"name": "bogus", "usage": "", "description": ""})
    m1["bogus"] = lambda: None
    c1[0]["usage"] = "tampered"
    c2, m2 = get_tools_config()
    assert [t["name"] for t in c2] == [t["name"] for t in c1][:-1], "caller mutation must not leak into cache"
    assert "bogus" not in m2, "caller mutation must not leak into cached map"
    assert c2[0]["usage"] != "tampered", "inner dict mutation must not poison the cache"
    print("✅ registry: cache reuse + returned containers isolated")


def test_gmail_alias_resolution():
    _, tools_map = get_tools_config()
    assert "tool_gmail_check" in tools_map, "alias tool_gmail_check must resolve (get_gmail_summary)"
    assert tools_map["tool_gmail_check"].__name__ == "get_gmail_summary"
    print("✅ registry: tool_gmail_check alias resolved")


def test_prompt_ordering():
    config_tools, _ = get_tools_config()
    names = [t["name"] for t in config_tools]
    assert names[0] == "tool_web_search"
    # Phase④: keyword/semantic 2エントリ → hybrid 1エントリに置換 (optionalなし=常に存在)
    assert names[1] == "tool_vault_hybrid_search"
    assert names[2] == "tool_create_note"
    print("✅ registry: prompt ordering preserved")


def test_engine_delegation_and_react_removed():
    import engine_plan_execute as engine
    e_config, e_map = engine._get_tools_config()
    r_config, r_map = get_tools_config()
    assert [t["name"] for t in e_config] == [t["name"] for t in r_config], "engine must delegate to registry"
    assert set(e_map) == set(r_map)
    assert not hasattr(engine, "run_react_loop"), "run_react_loop must be deleted"
    print("✅ engine: delegates to registry, run_react_loop removed")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
    if failed:
        print(f"\nFAILURE: {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nSUCCESS: all {len(tests)} tests passed")
