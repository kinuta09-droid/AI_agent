# -*- coding: utf-8 -*-
"""
Vault内frontmatterのtagsで半角スペースを含むものを検知し、_に置換する。
インラインリスト形式 (tags: [a b, c]) とブロックリスト形式 (tags:\n  - a b) の両方に対応。

Usage:
    py scripts/fix_tag_spaces.py            # dry-run (検知のみ、書き換えなし)
    py scripts/fix_tag_spaces.py --apply    # 実際に置換して保存
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.lib_shared import VAULT_ROOT

_FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)
_INLINE_TAGS_RE = re.compile(r'^(tags:\s*\[)(.*?)(\])', re.MULTILINE)
_BLOCK_TAGS_RE = re.compile(r'^(tags:\n)((?:  - .*\n)+)', re.MULTILINE)


def _fix_tag(tag: str) -> str:
    if '<%' in tag:  # Templater変数 (例: <% tp.file.tags %>) は展開前のテンプレートなので触らない
        return tag
    return tag.replace(' ', '_')


def _fix_inline(m):
    items = [t.strip() for t in m.group(2).split(',')]
    fixed = [_fix_tag(t) if t else t for t in items]
    return f"{m.group(1)}{', '.join(fixed)}{m.group(3)}"


def _fix_block(m):
    lines = m.group(2).splitlines(keepends=True)
    fixed_lines = []
    for line in lines:
        stripped = line.rstrip('\n')
        prefix, _, tag = stripped.partition('- ')
        fixed_lines.append(f"{prefix}- {_fix_tag(tag)}\n")
    return f"{m.group(1)}{''.join(fixed_lines)}"


def scan_and_fix(apply: bool):
    changed_files = []
    for root, _, files in os.walk(VAULT_ROOT):
        for fn in files:
            if not fn.endswith('.md'):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                print(f"[skip] {path}: {e}")
                continue

            fm_match = _FRONTMATTER_RE.match(content)
            if not fm_match:
                continue
            fm = fm_match.group(1)

            new_fm = _INLINE_TAGS_RE.sub(_fix_inline, fm)
            new_fm = _BLOCK_TAGS_RE.sub(_fix_block, new_fm)

            if new_fm != fm:
                changed_files.append(path)
                if apply:
                    new_content = content[:fm_match.start(1)] + new_fm + content[fm_match.end(1):]
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(new_content)

    return changed_files


def demo():
    assert _fix_tag("machine learning") == "machine_learning"
    assert _fix_tag("<% tp.file.tags %>") == "<% tp.file.tags %>"  # Templater変数は不可侵

    inline_before = "tags: [a b, c]"
    inline_after = _INLINE_TAGS_RE.sub(_fix_inline, inline_before)
    assert inline_after == "tags: [a_b, c]", inline_after

    block_before = "tags:\n  - a b\n  - c\n"
    block_after = _BLOCK_TAGS_RE.sub(_fix_block, block_before)
    assert block_after == "tags:\n  - a_b\n  - c\n", block_after

    print("[fix_tag_spaces] demo ok")


if __name__ == '__main__':
    if '--demo' in sys.argv:
        demo()
    else:
        apply = '--apply' in sys.argv
        changed = scan_and_fix(apply)
        mode = "APPLY" if apply else "DRY-RUN"
        print(f"[{mode}] {len(changed)} 件のノートでタグ内スペースを検知{'し修正しました' if apply else ''}:")
        for p in changed:
            print(f"  {os.path.relpath(p, VAULT_ROOT)}")
