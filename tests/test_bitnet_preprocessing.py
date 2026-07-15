# -*- coding: utf-8 -*-
"""BitNet 前処理フィルターテスト: 日本語入力 → 構造化英語出力"""
import sys
sys.path.insert(0, r'C:\AI_agent')
sys.stdout.reconfigure(encoding='utf-8')
from tools.tool_bitnet import _run_bitnet
import time, json

def test(title, prompt, max_tokens=30, temp=0.1):
    t0 = time.time()
    r = _run_bitnet(prompt, max_tokens=max_tokens, temperature=temp)
    elapsed = time.time() - t0
    print(f"[{elapsed:.1f}s] {title}")
    print(f"  → {r['text'].strip().splitlines()[0]}\n")

print("=" * 55)
print("CASE 1: 暗号通貨コマンド抽出  (数値・ラベル)")
print("  入力: 日本語の売買指示 → symbol/action/confidence")
print("=" * 55)
test(
    "BTC買い指示の構造化",
    'Extract trading info as JSON.\n'
    'Input: "ビットコインを今すぐ買って。確信度は高い。"\n'
    'Output JSON {symbol, action, confidence}:',
    max_tokens=25
)

test(
    "ETH警戒指示",
    'Extract trading info as JSON.\n'
    'Input: "イーサリアムはRSIが80超えてる。売りを検討すべき。"\n'
    'Output JSON {symbol, action, signal}:',
    max_tokens=25
)

print("=" * 55)
print("CASE 2: ログエラー分類  (severity/type ラベル)")
print("=" * 55)
test(
    "エラーログ分類",
    'Classify this Japanese log entry.\n'
    'Log: "APIキーが無効です。認証に失敗しました。接続を中断します。"\n'
    'Reply with: severity=HIGH/MEDIUM/LOW, type=AUTH/NETWORK/DATA/OTHER\n'
    'Answer:',
    max_tokens=20
)

test(
    "タイムアウトログ",
    'Classify this Japanese log entry.\n'
    'Log: "タイムアウトエラー。外部APIへの接続が30秒で切断されました。"\n'
    'Reply with: severity=HIGH/MEDIUM/LOW, type=AUTH/NETWORK/DATA/OTHER\n'
    'Answer:',
    max_tokens=20
)

print("=" * 55)
print("CASE 3: トピック抽出  (Gemini前処理の核心)")
print("  入力: 長い日本語テキスト → 短い英語トピックリスト")
print("=" * 55)
test(
    "長文からキーワード3つ抽出",
    'Extract 3 key English topics from this Japanese text.\n'
    'Text: "今日のBTC相場は95000ドルを突破し、RSIが過熱圏に入った。一方でFRBの利上げ'
    '観測が再燃しており、機関投資家の売り圧力が強まっている。テクニカル的には次の抵抗線'
    'は100000ドルだが、マクロ環境のリスクを考慮すると慎重な姿勢が必要だ。"\n'
    'Topics (comma-separated):',
    max_tokens=20
)

test(
    "Discordメッセージのトピック分類",
    'What is this Japanese Discord message about? Reply with ONE English category.\n'
    'Categories: [crypto-price, news, question, error-report, announcement]\n'
    'Message: "さっきBotが落ちた。ログ見たらAPIレート制限のエラーだった。"\n'
    'Category:',
    max_tokens=10
)

print("=" * 55)
print("CASE 4: 数値・固有名詞抽出  (言語非依存)")
print("=" * 55)
test(
    "価格・数値抽出",
    'Extract numbers and crypto symbols from this Japanese text.\n'
    'Text: "BTCが95000ドル、ETHが3200ドル。SOLは200ドルを割り込んだ。"\n'
    'Format: SYMBOL:PRICE, ...\n'
    'Answer:',
    max_tokens=25
)
