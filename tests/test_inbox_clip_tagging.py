# -*- coding: utf-8 -*-
"""
Obsidian Web Clipper連携 (00_inbox自動タグ付け/MOC分類) の回帰テスト。

対象: tools/tool_obsidian.py の tag_untagged_clips

方針: 実Vault・実BitNet・実Gemini I/Oは一切禁止。tempfileの一時ディレクトリに
偽ノートを書いて検証する。generate_note_tags/_determine_parent_moc/update_vault_index
はモジュール属性差し替えでモックする (tag_untagged_clips内部はいずれも遅延import
または直接呼び出しのため、呼び出し元モジュールの属性を差し替えれば効く)。

実行: py tests/test_inbox_clip_tagging.py
"""
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding='utf-8')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import tools.tool_obsidian as obs_mod
import tools.tool_bitnet as bitnet_mod


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _make_clip_note(path: str, source: str = "https://example.com/article") -> None:
    content = (
        "---\n"
        f"source: {source}\n"
        "tags:\n"
        "  - clippings\n"
        "---\n\n# Article\n\nBody text.\n"
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _make_fleeting_note(path: str) -> None:
    content = "---\ntags:\n  - type/fleeting\n---\n\n# Idea\n\nBody text.\n"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _make_inline_tags_clip_note(path: str) -> None:
    content = (
        "---\n"
        "source: https://example.com/article\n"
        "tags: [clippings, web]\n"
        "---\n\n# Article\n\nBody text.\n"
    )
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _make_no_tags_key_clip_note(path: str) -> None:
    content = "---\nsource: https://example.com/article\n---\n\n# Article\n\nBody text.\n"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


class _Patch:
    """複数モジュール属性の一時差し替えをまとめて元に戻すヘルパー。"""

    def __init__(self):
        self._saved = []

    def set(self, module, name, value):
        self._saved.append(("attr", module, name, getattr(module, name)))
        setattr(module, name, value)

    def set_sys_module(self, key: str, value):
        self._saved.append(("sysmod", None, key, sys.modules.get(key)))
        sys.modules[key] = value

    def restore(self):
        for kind, module, name, value in reversed(self._saved):
            if kind == "attr":
                setattr(module, name, value)
            else:
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


def _fake_update_vault_index_module(patch: "_Patch", counter: dict):
    mod = types.ModuleType("tools.tool_vault_index")
    mod.update_vault_index = lambda: counter.__setitem__("n", counter.get("n", 0) + 1)
    patch.set_sys_module("tools.tool_vault_index", mod)
    return mod


def _spy(counter: dict, key: str, return_value):
    def _fn(*args, **kwargs):
        counter[key] = counter.get(key, 0) + 1
        counter[f"{key}_args"] = args
        return return_value
    return _fn


# ---------------------------------------------------------
# (a) source: 持ちノートのみClip判定される
# ---------------------------------------------------------

def test_tag_untagged_clips_only_processes_clipper_notes():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        fleeting_path = os.path.join(inbox, "fleeting.md")
        _make_clip_note(clip_path)
        _make_fleeting_note(fleeting_path)

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", lambda title, preview: ["tagx"])
            patch.set(obs_mod, "_determine_parent_moc", lambda content: None)
            _fake_update_vault_index_module(patch, counter)

            result = obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        assert result == "Tagged 1 clip note(s).", result
        assert "clip/processed" in _read(clip_path)
        assert "clip/processed" not in _read(fleeting_path), "source:の無いノートまで処理された"
        assert counter.get("n") == 1, "タグ付けが1件あったのにupdate_vault_indexが呼ばれていない"
    print("✅ [1] tag_untagged_clips: source:持ちノートのみ処理され、検索インデックスも更新される")


# ---------------------------------------------------------
# (b) clip/processed済みは2回目実行でスキップ (冪等性)
# ---------------------------------------------------------

def test_tag_untagged_clips_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        _make_clip_note(clip_path)

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", lambda title, preview: ["tagx"])
            patch.set(obs_mod, "_determine_parent_moc", lambda content: None)
            _fake_update_vault_index_module(patch, counter)

            first = obs_mod.tag_untagged_clips()
            second = obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        assert first == "Tagged 1 clip note(s).", first
        assert second == "Tagged 0 clip note(s).", second
        assert counter.get("n") == 1, f"2回目実行で無駄にupdate_vault_indexが呼ばれた: {counter}"
    print("✅ [2] tag_untagged_clips: 2回目実行は0件・インデックス更新も呼ばれない (冪等性)")


# ---------------------------------------------------------
# (c) tags:がYAMLブロック形式でないノートはBitNet/Geminiを呼ばずスキップされる
#     (呼んでもclip/processedを書けず、毎日課金だけ発生するコストリークを防ぐ)
# ---------------------------------------------------------

def test_tag_untagged_clips_inline_tags_skipped_without_backend_calls():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        _make_inline_tags_clip_note(clip_path)

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", _spy(counter, "tags_called", ["tagx"]))
            patch.set(obs_mod, "_determine_parent_moc", _spy(counter, "moc_called", None))
            _fake_update_vault_index_module(patch, counter)

            result = obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        assert result == "Tagged 0 clip note(s).", result
        assert "clip/processed" not in _read(clip_path)
        assert counter.get("tags_called") is None, "書き込めないノートなのにBitNetが呼ばれた(無駄コスト)"
        assert counter.get("moc_called") is None, "書き込めないノートなのにGeminiが呼ばれた(無駄課金)"
    print("✅ [3] tag_untagged_clips: インライン配列tagsはBitNet/Geminiを呼ばずスキップ")


def test_tag_untagged_clips_missing_tags_key_skipped_without_backend_calls():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        _make_no_tags_key_clip_note(clip_path)

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", _spy(counter, "tags_called", ["tagx"]))
            patch.set(obs_mod, "_determine_parent_moc", _spy(counter, "moc_called", None))
            _fake_update_vault_index_module(patch, counter)

            result = obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        assert result == "Tagged 0 clip note(s).", result
        assert counter.get("tags_called") is None
        assert counter.get("moc_called") is None
    print("✅ [3b] tag_untagged_clips: tags:キー自体が無いノートもBitNet/Geminiを呼ばずスキップ")


# ---------------------------------------------------------
# (d) BitNet/Gemini相当が両方例外でもクラッシュせずclip/processedが付く
# ---------------------------------------------------------

def test_tag_untagged_clips_survives_both_backends_failing():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        _make_clip_note(clip_path)

        def _boom_tags(title, preview):
            raise RuntimeError("BitNet down")

        def _boom_moc(content):
            raise RuntimeError("Gemini down")

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", _boom_tags)
            patch.set(obs_mod, "_determine_parent_moc", _boom_moc)
            _fake_update_vault_index_module(patch, counter)

            result = obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        assert result == "Tagged 1 clip note(s).", result
        assert "clip/processed" in _read(clip_path), "両バックエンド失敗時にprocessedマーカーが付いていない"
        assert counter.get("n") == 1
    print("✅ [4] tag_untagged_clips: BitNet/Gemini両方失敗してもクラッシュせずclip/processedが付く")


# ---------------------------------------------------------
# (e) limitに達したら打ち切られる
# ---------------------------------------------------------

def test_tag_untagged_clips_respects_limit():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        for i in range(5):
            _make_clip_note(os.path.join(inbox, f"clip{i}.md"))

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", lambda title, preview: ["tagx"])
            patch.set(obs_mod, "_determine_parent_moc", lambda content: None)
            _fake_update_vault_index_module(patch, counter)

            result = obs_mod.tag_untagged_clips(limit=2)
        finally:
            patch.restore()

        assert result == "Tagged 2 clip note(s).", result
    print("✅ [5] tag_untagged_clips: limit到達で打ち切られる")


# ---------------------------------------------------------
# (f) LLMに渡すpreviewはfrontmatterを含まない本文だけであること
# ---------------------------------------------------------

def test_tag_untagged_clips_preview_excludes_frontmatter():
    with tempfile.TemporaryDirectory() as d:
        inbox = os.path.join(d, "00_inbox")
        os.makedirs(inbox)
        clip_path = os.path.join(inbox, "clip.md")
        _make_clip_note(clip_path)

        patch = _Patch()
        counter = {}
        try:
            patch.set(obs_mod, "WORK_DIR", d)
            patch.set(bitnet_mod, "generate_note_tags", _spy(counter, "tags_called", ["tagx"]))
            patch.set(obs_mod, "_determine_parent_moc", _spy(counter, "moc_called", None))
            _fake_update_vault_index_module(patch, counter)

            obs_mod.tag_untagged_clips()
        finally:
            patch.restore()

        tags_args = counter["tags_called_args"]
        moc_args = counter["moc_called_args"]
        assert "source:" not in tags_args[1], f"generate_note_tagsにfrontmatterが漏れている: {tags_args}"
        assert "Body text." in tags_args[1], f"generate_note_tagsに本文が渡っていない: {tags_args}"
        assert "source:" not in moc_args[0], f"_determine_parent_mocにfrontmatterが漏れている: {moc_args}"
        assert "Body text." in moc_args[0], f"_determine_parent_mocに本文が渡っていない: {moc_args}"
    print("✅ [6] tag_untagged_clips: LLMに渡すpreviewはfrontmatterを含まない本文だけ")


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
