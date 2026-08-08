# -*- coding: utf-8 -*-
"""
Skill Digest — Skill Radar が見つけた候補1件を SNS/note 下書きにする

Flow:
  1. tool_skill_radar が残した候補 (logs/skill_radar_latest.json) から1件選ぶ
     (候補が無ければその場で Skill Radar を回す / --repo で full_name 直接指定も可)
  2. description/topics/comment_ja/ai_fit から直接 digest dict を組み立てる
     (**新しい Gemini 要約ステージは追加しない** — Skill Radar が
     evaluate_fit_ja() で既に付与済みの適合度コメントをそのまま使う設計)
  3. tool_paper_digest.build_sns_post / build_note_article を再利用して
     X(Twitter) ティザー投稿下書き / note.com・X Articles 記事下書きを生成
  4. Obsidian に下書きを保存

「投稿」まで自動化しない理由 (tool_paper_digest.py と同じ設計判断):
  下書きの生成と保存までを担当し、投稿は人間の確認を挟む。

再利用の設計 (tool_document.py と同じパターン):
  build_sns_post/build_note_article はプロンプトの語彙 (subject_label/
  source_label) や digest スキーマの差 (fallback_fields) を「paper 形」の
  アダプタ辞書経由で吸収できるよう汎用化済み。ここではその型を Skill 用に
  合わせるだけで、プロンプト文言 (SNS_HOOK_PATTERNS 等) は一切複製しない。

既知の非対称 (mark_seen のタイミング):
  Skill Radar の mark_seen() は save 引数と無関係に「一覧に出した時点」で
  無条件に呼ばれる既存仕様 (tool_skill_radar.py のモジュールdocstring参照)。
  そのため _resolve_skill() が候補ゼロでバックフィル実行すると、これから
  digest 化しようとしている候補も含めて次回 Radar では既出扱いになる。
  paper_digest の _resolve_paper (バックフィルしても save=False なら
  paper側は既出にならない) とは挙動が異なる点として明示しておく。

Usage:
  py tools/tool_skill_digest.py                       # 候補1位を digest
  py tools/tool_skill_digest.py --index 2 --mode note
  py tools/tool_skill_digest.py --repo owner/name --mode both
  py tools/tool_skill_digest.py --no-save
"""
import argparse
import datetime
import os
import sys

# プロジェクトルートを sys.path に追加（tools/ 直接実行時対応）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

from tools.tool_paper_digest import build_note_article, build_sns_post, slugify
from tools.tool_skill_radar import (
    SKILLS_REPORT_DIR,
    load_latest_candidates,
    mark_seen,
    run_skill_radar,
)


# ---------------------------------------------------------
# 純関数 (テスト対象の中核)
# ---------------------------------------------------------

def _skill_adapter(repo: dict) -> dict:
    """build_sns_post/build_note_article へ渡す「paper 形の」アダプタ辞書。

    tool_document.py の _draft_subject_meta と同じパターン。GitHub リポジトリは
    常に恒久的な公開URL (html_url) を持つため、paper/document 版と違い
    abs_url を空にする分岐は不要。
    """
    return {
        "title": repo.get("full_name", ""),
        "abs_url": repo.get("html_url", ""),
        "subject_label": "Skill",
        "source_label": "リポジトリ",
        "fallback_fields": [("説明", "description"), ("導入価値", "why_fit")],
    }


def _build_skill_digest(repo: dict) -> dict:
    """description/topics/comment_ja/ai_fit から直接 digest dict を組み立てる。

    comment_ja (Gemini適合度判定のコメント) が最有力の材料だが、--no-comment で
    回した週や evaluate_fit_ja() 失敗時は欠けるため、description → full_name の
    順にフォールバックし、例外にはしない。
    """
    description = (repo.get("description") or "").strip()
    comment = (repo.get("comment_ja") or "").strip()
    one_liner = comment or description or repo.get("full_name", "")
    why_fit = comment or "(AI適合度コメントなし。説明文とtopicsから判断してください)"
    return {
        "one_liner": one_liner,
        "description": description,
        "why_fit": why_fit,
        "fit": repo.get("ai_fit") or "unknown",
        "stars": repo.get("stars", 0),
        "keywords": list(repo.get("topics") or [])[:5],
    }


def render_skill_draft(full_name: str, sns_post: str, article_title: str, article_body: str) -> str:
    """下書きノート本文を組み立てる純関数 (tool_document.render_document_draft と同型)。"""
    lines = [f"# ✍️ Skill紹介 下書き — {full_name}", ""]
    if sns_post:
        lines += [f"## X (Twitter) 投稿 ({len(sns_post)}字)", "", sns_post, ""]
    if article_body:
        lines += ["## note.com 記事下書き", "",
                  f"**タイトル案:** {article_title}", "", article_body, "",
                  "## X Articles 下書き (note.com と同内容、X内で読める長文記事)", "",
                  f"**タイトル案:** {article_title}", "", article_body]
    return "\n".join(lines)


# ---------------------------------------------------------
# 取得・生成
# ---------------------------------------------------------

def _resolve_skill(index: int = 1, repo: str = "") -> dict:
    """digest 対象のリポジトリ dict を決める。候補が無ければレーダーを回す。

    repo (owner/name) 指定時は候補一覧内から大文字小文字無視で full_name 一致検索する
    (新規GitHub API呼び出しを増やさない — 候補一覧に無いリポジトリは
    comment_ja/ai_fit が無く digest の材料が薄すぎるため対象外)。
    """
    candidates = load_latest_candidates()
    if repo:
        for c in candidates:
            if c.get("full_name", "").lower() == repo.lower():
                return c
        raise ValueError(
            f"{repo} は現在の候補一覧に見つかりません "
            f"(Skill Radar を先に実行するか、候補一覧にある full_name を指定してください)"
        )

    radar_msg = ""
    if not candidates:
        print("[SkillDigest] 候補が無いので Skill Radar を実行します...")
        radar_msg = run_skill_radar(top_k=5, save=False)
        candidates = load_latest_candidates()
    if not candidates:
        raise ValueError(f"候補リポジトリがありません — Skill Radar の結果: {radar_msg or '(未実行)'}")
    if index < 1 or index > len(candidates):
        raise ValueError(f"index は 1〜{len(candidates)} の範囲で指定してください (指定: {index})")
    return candidates[index - 1]


# ---------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------

def run_skill_digest(index: int = 1, repo: str = "", mode: str = "sns", save: bool = True) -> str:
    """Skill Radar 候補1件をSNS/note下書きにする
    (ツール層の戻り値契約: 失敗は "Error: ...")。"""
    print(f"[Tool] 📝 Skill Digest (index={index}, repo={repo or '-'}, mode={mode})")
    try:
        skill = _resolve_skill(index=index, repo=repo)
    except Exception as e:
        print(f"[SkillDigest] ❌ 対象リポジトリの特定に失敗: {e}")
        return f"Error: 対象リポジトリを特定できませんでした ({e})"

    digest = _build_skill_digest(skill)
    adapter = _skill_adapter(skill)

    sns_post = ""
    if mode in ("sns", "both"):
        try:
            sns_post = build_sns_post(digest, adapter)
        except Exception as e:
            print(f"[SkillDigest] ❌ X投稿の生成に失敗: {e}")
            return f"Error: X投稿の生成に失敗しました ({e})"

    article_title, article_body = "", ""
    if mode in ("note", "both"):
        try:
            article = build_note_article(digest, adapter, {})
        except Exception as e:
            print(f"[SkillDigest] ❌ note記事の生成に失敗: {e}")
            return f"Error: note記事の生成に失敗しました ({e})"
        article_title = article.get("title") or skill.get("full_name", "")
        article_body = article.get("body", "")

    now = datetime.datetime.now()
    slug = slugify(skill.get("full_name", "").replace("/", "_"))
    saved_path = ""
    if save and (sns_post or article_body):
        draft_body = render_skill_draft(skill.get("full_name", ""), sns_post, article_title, article_body)
        try:
            os.makedirs(SKILLS_REPORT_DIR, exist_ok=True)
            saved_path = os.path.join(SKILLS_REPORT_DIR, f"{now:%Y-%m-%d}_{slug}_draft.md")
            with open(saved_path, "w", encoding="utf-8") as f:
                f.write(draft_body)
            print(f"[SkillDigest] ✅ Saved: {saved_path}")
        except Exception as e:
            print(f"[SkillDigest] ❌ 下書きの保存に失敗: {e}")
            return f"Error: 下書きの保存に失敗しました ({e})"

    # 保存まで到達したときだけ既出にする。paper_digest は save=True なら要約ノートを
    # 常に書くので `if save:` で足りるが、Skill Digest には「常に書く」成果物が無い
    # (mode="none" は saved_path が空のまま) ため、save ではなく saved_path で判定する
    # (--no-save や mode="none" の試し打ちで既出にすると、1文字も残っていない候補が消える)。
    if saved_path:
        mark_seen([skill.get("full_name", "")])

    parts = [
        f"Skill Digest: 「{skill.get('full_name', '')}」",
        f"要約: {digest.get('one_liner', '')}",
    ]
    if sns_post:
        parts.append(f"【Xティザー投稿下書き】({len(sns_post)}字)\n{sns_post}")
    if article_body:
        parts.append(f"【note/X Articles 記事下書き】タイトル案: {article_title} / {len(article_body)}字を生成しました")
    if saved_path:
        parts.append(f"保存先: [[40_Report/Skills/{os.path.basename(saved_path)}]]")
    return "\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Skill Digest — Skill Radar候補のSNS/note下書き生成")
    parser.add_argument("--index", type=int, default=1, help="Skill Radar 候補の番号 (1始まり)")
    parser.add_argument("--repo", default="", help="候補一覧内の full_name を直接指定 (例: owner/name)")
    parser.add_argument("--mode", default="sns", choices=["sns", "note", "both", "none"])
    parser.add_argument("--no-save", action="store_true", help="Obsidian に書かない")
    args = parser.parse_args()

    result = run_skill_digest(
        index=args.index, repo=args.repo, mode=args.mode, save=not args.no_save,
    )
    print("\n" + result)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
