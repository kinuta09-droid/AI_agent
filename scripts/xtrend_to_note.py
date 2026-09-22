# -*- coding: utf-8 -*-
"""
XTrend → note.com 記事変換スクリプト

最新の XTrend レポート (Obsidian) を読み込み、
Gemini でnote.com向け記事を生成して scripts/note_draft.md に保存する。

Usage:
  py scripts/xtrend_to_note.py
  py scripts/xtrend_to_note.py --file D:/path/to/specific_xtrend.md
"""

import os
import sys
import glob
import re
import argparse
import datetime

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv

from lib.paths import ENV_PATH

load_dotenv(ENV_PATH)

from lib.ja_style_guard import enforce_style
from lib.lib_shared import client, MODEL_NAME_HEAVY, WORK_DIR

XTREND_DIR = os.path.join(WORK_DIR, "40_Report", "XTrend")
OUTPUT_PATH = os.path.join(_PROJECT_ROOT, "scripts", "note_draft.md")


# ---------------------------------------------------------
# STEP 1: 最新レポートを取得
# ---------------------------------------------------------

def find_latest_xtrend(specific_file: str = None) -> str:
    """最新の xtrend.md ファイルパスを返す。"""
    if specific_file:
        return specific_file

    pattern = os.path.join(XTREND_DIR, "*_xtrend.md")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"XTrendレポートが見つかりません: {XTREND_DIR}")
    return files[-1]  # 最新


def load_xtrend_markdown(filepath: str) -> str:
    with open(filepath, encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------
# STEP 2: 上位機能を抽出 (Markdown パース)
# ---------------------------------------------------------

def extract_top_features(md_text: str, top_n: int = 5) -> list[dict]:
    """
    Markdownから各機能のデータを抽出し、エンゲージメント上位N件を返す。
    スコアは '★★★★★ (5/5)' のような表記から数値を取り出す。
    """
    # ### NN. 機能名 のブロックで分割
    blocks = re.split(r'\n### \d+\. ', md_text)
    features = []

    for block in blocks[1:]:  # 最初のヘッダ部分はスキップ
        lines = block.strip().split('\n')
        feature_name = lines[0].strip()

        def get_field(label: str) -> str:
            for line in lines:
                if f'| {label}' in line:
                    # | 項目 | 内容 | の形式から「内容」を取り出す
                    parts = line.split('|')
                    if len(parts) >= 3:
                        return parts[2].strip()
            return ''

        arms_raw = get_field('ARMS親和性')
        # '★★★★★ (5/5)' → 5
        m = re.search(r'\((\d)/5\)', arms_raw)
        arms_score = int(m.group(1)) if m else 0

        diff_raw = get_field('実装難易度')
        m2 = re.search(r'\((\d)/5\)', diff_raw)
        diff_score = int(m2.group(1)) if m2 else 0

        # engagement_score は Gemini が出力するので "7597.0" 等の揺れを許容する
        m3 = re.search(r'\d+', get_field('エンゲージメント'))
        eng_score = int(m3.group()) if m3 else 0

        url = get_field('元投稿')

        features.append({
            'name': feature_name,
            'summary': get_field('概要'),
            'problem': get_field('解決課題'),
            'stack': get_field('技術スタック'),
            'difficulty': diff_score,
            'arms': arms_score,
            'engagement': eng_score,
            'rationale': get_field('ARMS評価理由'),
            'url': url,
        })

    # 記事は一般読者向けなので「話題になっている順」で選ぶ。ARMS親和性順だと
    # 導入済みの技術 (Claude Code/MCP 等、読者の関心は最も高い) が★1〜2 で
    # 落ちてしまう (2026-09-23 arms_affinity に ARMS 構成を渡した副作用)。
    # エンゲージメント行の無い旧レポートは全件0になり、安定ソートで元の順を保つ。
    features.sort(key=lambda x: -x['engagement'])
    return features[:top_n]


# ---------------------------------------------------------
# STEP 3: Gemini で記事生成
# ---------------------------------------------------------

ARTICLE_PROMPT = """あなたはAIテクノロジーを平易に解説するライターです。
以下のAIトレンドデータをもとに、note.com向けの日本語記事を書いてください。

## 要件
- ターゲット: AIに興味があるが技術者ではない人（エンジニアも読む）
- トーン: 熱意があり、読みやすく、かつ具体的
- 文字数: 1500〜2500文字
- 有料記事の「無料部分」として機能すること（冒頭300字で引きつける）

## 記事の構成
1. **タイトル** (20〜30字、インパクト重視)
2. **リード文** (150〜200字、今週AIで何が起きたかを要約し、読む理由を作る)
3. **本編: TOP {top_n} 技術の解説** (各200〜300字、具体的な使いどころを含む)
4. **まとめ** (150字、今週のトレンドの意義を1段落で)
5. **次回予告** (50字程度、次号を読みたくなる一言)
6. **フッター** (このレポートはAIがX(Twitter)を自動分析して生成した旨と、筆者の一言)

## 注意
- 各技術のURLは「詳しくはこちら→ [URL]」の形式で本文中に1回だけ含める
- 専門用語には簡単な括弧説明を入れる
- 「〜です/ます」体で統一
- 「---」区切り線は使わず、見出し(##, ###)で区切る

## トレンドデータ
{features_text}

今日の日付: {today}
"""


def build_features_text(features: list[dict]) -> str:
    lines = []
    for i, f in enumerate(features, 1):
        lines.append(f"【{i}】{f['name']}")
        lines.append(f"  概要: {f['summary']}")
        lines.append(f"  解決課題: {f['problem']}")
        lines.append(f"  技術: {f['stack']}")
        lines.append(f"  難易度: {f['difficulty']}/5")
        lines.append(f"  ARMS親和性: {f['arms']}/5")
        lines.append(f"  URL: {f['url']}")
        lines.append("")
    return '\n'.join(lines)


def generate_article(features: list[dict]) -> str:
    features_text = build_features_text(features)
    today = datetime.date.today().isoformat()
    prompt = ARTICLE_PROMPT.format(
        top_n=len(features),
        features_text=features_text,
        today=today,
    )

    print("[xtrend_to_note] Gemini で記事を生成中...")
    resp = client.models.generate_content(
        model=MODEL_NAME_HEAVY,
        contents=prompt,
    )
    return enforce_style(resp.text or "", prompt, MODEL_NAME_HEAVY)


# ---------------------------------------------------------
# STEP 4: 保存
# ---------------------------------------------------------

def save_draft(article: str, source_file: str) -> str:
    today = datetime.date.today().isoformat()
    header = f"<!-- 元データ: {source_file} | 生成日: {today} -->\n\n"
    content = header + article

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

    return OUTPUT_PATH


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='XTrend → note.com 記事変換')
    parser.add_argument('--file', default=None, help='使用するXTrendファイルのパス')
    parser.add_argument('--top', type=int, default=5, help='取り上げる機能の数 (デフォルト: 5)')
    args = parser.parse_args()

    # Step 1: ファイル取得
    filepath = find_latest_xtrend(args.file)
    print(f"[xtrend_to_note] 読み込み: {filepath}")

    # Step 2: データ抽出
    md = load_xtrend_markdown(filepath)
    features = extract_top_features(md, top_n=args.top)
    print(f"[xtrend_to_note] 上位 {len(features)} 件を抽出:")
    for i, f in enumerate(features, 1):
        print(f"  {i}. {f['name']} (ARMS:{f['arms']}/5, 難易度:{f['difficulty']}/5)")

    # Step 3: 記事生成
    article = generate_article(features)

    # Step 4: 保存
    out = save_draft(article, filepath)
    print(f"\n[xtrend_to_note] ✅ 記事ドラフトを保存しました: {out}")
    print(f"\n{'='*60}")
    print("次のステップ:")
    print("  1. scripts/note_draft.md を開いて内容を確認")
    print("  2. https://note.com/notes/new でペースト")
    print("  3. 有料設定: ¥300〜500、最初の300文字を無料公開")
    print("  4. タグ: #AI #AIエージェント #テクノロジー #週次レポート")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
