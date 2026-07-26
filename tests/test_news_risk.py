# -*- coding: utf-8 -*-
"""
lib/quant_core.classify_news_risk のユニットテスト。

背景: 旧実装は risk_keywords の部分文字列一致で、"sec" が
"seconds"/"security"/"sector" に、"war" が "warning" に当たっていた。
SYSTEMIC_RISK は _decide_signal の最優先分岐でテクニカルを無効化する
(STAY AWAY / confidence=0) ため、2026-03〜07 のライブ分析でニュース検索が
走った9件のうち7件が誤爆で STAY AWAY になり、trade_log が5ヶ月ゼロ件だった。

ここでは「実際に誤爆した記事タイトル」を回帰ケースとして固定する。
実API・実Vaultは使わない(純関数のみ)。
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')

from lib.quant_core import classify_news_risk, _decide_signal

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def art(title, content=""):
    return {"title": title, "url": "https://example.com/x", "content": content}


def risk_of(articles, coin="BTC"):
    _lines, level = classify_news_risk(articles, coin)
    return level


# ---------------------------------------------------------
# 回帰: 実際に STAY AWAY を誤爆させた記事 (Vault の Crypto_*.md より)
# ---------------------------------------------------------

def test_real_false_positives():
    print("[Test] 実データ回帰: 誤爆した記事が SYSTEMIC_RISK にならない")

    # INJ 2026-07-26 (-6.4%) — この3本で STAY AWAY になっていた
    inj = [
        art("Crypto News Today: AlphaPepe Presale Nears Sell Out As Bitcoin "
            "Price Prediction Eyes $200,000",
            "The presale sold out in seconds as traders rotate into the sector."),
        art("Crypto News Today: AlphaPepe Nears Tier-1 CEX Partnership Reveal "
            "as Ethereum Price Prediction Eyes $8,500",
            "Exchange listing expected within seconds of the announcement."),
        art("Gold gains capped as markets ignore positive data over Hormuz "
            "worries, Indian silver premiums rise despite low demand",
            "Bullion traders watched the security situation in the region."),
    ]
    check("INJ 2026-07-26 の3本 → SYSTEMIC_RISK にならない",
          risk_of(inj, "INJ") != "SYSTEMIC_RISK")

    # DOGE 2026-07-24 (-4.5%)
    doge = [
        art("Crypto News Today: AlphaPepe Presale Nears Sell Out",
            "Sold out in seconds."),
        art("Watch Out: Plenty of Economic Developments and Altcoin Events "
            "This Week—Here's the Day-by-Day, Hour-by-Hour Schedule",
            "Analysts are warning of volatility in the crypto sector."),
    ]
    check("DOGE 2026-07-24 の記事 → SYSTEMIC_RISK にならない",
          risk_of(doge, "DOGE") != "SYSTEMIC_RISK")

    # INJ 2026-02-22 (+9.6%) — 上昇局面なのに STAY AWAY だった
    inj_up = [
        art("Crypto price news: BTC unfazed by Trump tariff news; DOGE, SOL, "
            "ADA lead modest bounce",
            "Crypto majors advanced as the sector recovered."),
        art("How Regulation And Data Collection Are Creating Physical "
            "Security Risks",
            "Security researchers discussed data collection practices."),
        art("Daily Market Update: Stock Futures Drop, Bitcoin Steady at $68K "
            "Ahead of Fed Data",
            "Traders are warning that seconds matter ahead of the print."),
    ]
    check("INJ 2026-02-22 (+9.6%) → SYSTEMIC_RISK にならない",
          risk_of(inj_up, "INJ") != "SYSTEMIC_RISK")

    # 純粋な強気記事 (旧実装は "this sector" の sec で誤爆した)
    bullish = [art("Here's Why Bitcoin Is Making An Incredible Rebound Wednesday, "
                   "Surging More than 7%",
                   "Bitcoin led the sector higher, gaining in a matter of seconds.")]
    check("強気記事のみ → NEUTRAL", risk_of(bullish) == "NEUTRAL")


def test_substring_traps():
    print("[Test] 部分文字列トラップが単語境界で外れる")
    traps = {
        "seconds": "The move happened in seconds for bitcoin traders.",
        "security": "Crypto exchange security practices were reviewed.",
        "sector": "The crypto sector rallied today.",
        "securities": "Tokenized securities gained traction in crypto.",
        "warning": "Analysts are warning about bitcoin volatility.",
        "toward": "Bitcoin moved toward its previous high.",
        "banks": "Banks are adopting blockchain rails.",
        "abandon": "Traders abandon the bitcoin short trade.",
        "urban": "Urban adoption of crypto payments grew.",
    }
    for word, text in traps.items():
        check(f"'{word}' で誤爆しない", risk_of([art(text, text)]) == "NEUTRAL")


def test_true_positives():
    print("[Test] 本物の深刻材料は SYSTEMIC_RISK になる")
    cases = {
        "取引所ハック": "Major crypto exchange hacked, $200M in bitcoin stolen",
        "破産": "Crypto lender files for bankruptcy after withdrawal freeze",
        "詐欺": "Token founder charged with fraud in crypto scheme",
        "規制禁止": "China bans crypto mining nationwide",
        "上場廃止": "Binance delisting the token after review",
        "SEC訴訟(大文字)": "SEC sues major crypto exchange over unregistered offering",
        "sec lawsuit(小文字)": "New sec lawsuit targets a crypto issuer",
        "エクスプロイト": "DeFi protocol exploited for $80M in an attack",
        "ラグプル": "Investors lose funds in a crypto rug pull",
    }
    for name, text in cases.items():
        check(f"{name} → SYSTEMIC_RISK",
              risk_of([art(text, text)]) == "SYSTEMIC_RISK")


def test_relevance_gate():
    print("[Test] 関連性ゲート: 暗号資産と無関係な記事は採点しない")
    unrelated = [art("Gold and silver premiums rise on Hormuz war worries",
                     "Bullion demand climbed as the war premium widened.")]
    check("金・銀の戦争記事は暗号資産と無関係 → NEUTRAL",
          risk_of(unrelated, "INJ") == "NEUTRAL")

    lines, _ = classify_news_risk(unrelated, "INJ")
    check("除外理由がレポート行に残る", any("対象外" in ln for ln in lines))

    # 銘柄名で関連付くケース (crypto 系語が無くても拾う)
    named = [art("INJ delisting announced by the venue",
                 "INJ will be removed from the venue next week.")]
    check("銘柄名一致なら関連ありと判定 → SYSTEMIC_RISK",
          risk_of(named, "INJ") == "SYSTEMIC_RISK")

    # 関連性ゲート自体も単語境界で判定する。部分文字列にすると "eth" が
    # whether/together/method に、"coin" が coincidence に当たって素通しになり、
    # 深刻語側で潰したのと同じ罠をゲート側で作り直すことになる
    traps = {
        "whether/eth": "Oil traders debated whether the war would escalate.",
        "together/eth": "Officials met together as the war dragged on.",
        "method/eth": "A new method of banking was proposed amid the war.",
        "coincidence/coin": "By coincidence the war news hit the same day.",
    }
    for name, text in traps.items():
        check(f"関連性ゲートが '{name}' で素通ししない",
              risk_of([art(text, text)], "INJ") == "NEUTRAL")


def test_scoring_levels():
    print("[Test] スコア段階 (閾値は旧実装から据え置き)")
    fud = [art("Bitcoin sell-off continues as traders take profit",
               "A pullback in crypto followed the rally.")]
    check("弱材料1本 → NORMAL_CORRECTION",
          risk_of(fud) == "NORMAL_CORRECTION")

    check("記事ゼロ → NEUTRAL", risk_of([]) == "NEUTRAL")

    lines, _ = classify_news_risk(
        [art("Crypto exchange hacked", "Crypto exchange hacked for $10M")], "BTC")
    check("判定根拠がレポート行に残る (旧実装は根拠ゼロだった)",
          any("深刻語ヒット" in ln for ln in lines))


def test_gate_wiring():
    print("[Test] _decide_signal との結線")
    sig, conf, stype, _r = _decide_signal(
        "SYSTEMIC_RISK", False, True, True, 100.0, 10, 20.0, "SIDEWAYS", "UP", 20.0)
    check("SYSTEMIC_RISK は STAY AWAY / confidence=0 のまま",
          sig == "STAY AWAY" and conf == 0.0)

    sig2, _c2, _s2, _r2 = _decide_signal(
        "NEUTRAL", False, True, True, 100.0, 10, 20.0, "SIDEWAYS", "UP", 20.0)
    check("NEUTRAL なら sweep が STRONG BUY まで通る", sig2 == "STRONG BUY")


if __name__ == "__main__":
    print("=" * 60)
    print("classify_news_risk テスト")
    print("=" * 60)
    test_real_false_positives()
    test_substring_traps()
    test_true_positives()
    test_relevance_gate()
    test_scoring_levels()
    test_gate_wiring()
    print("=" * 60)
    print(f"PASS: {PASS} / FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
