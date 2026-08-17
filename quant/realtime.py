import argparse
import json
import urllib.request
from datetime import datetime

from data_fetcher import fetch_realtime, sina_symbol


def fetch_minute_kline(code):
    symbol = sina_symbol(code)
    url = (
        f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&datalen=120&scale=60&ma=no"
    )
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode("gbk"))
    rows = []
    for item in data:
        rows.append(
            {
                "time": item["day"],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item.get("volume", 0)),
            }
        )
    return rows


def compute_signals(code, price):
    """基于历史K线计算策略买卖信号（简化版），返回可读字符串。"""
    import strategy_engine as se

    sig = se.compute_basic_signals(code, price)
    if not sig:
        return "数据不足"

    signals = []
    signals.append("MA5上方" if sig["above_ma5"] else "MA5下方")
    signals.append("MACD多头" if sig["macd_bull"] else "MACD空头")
    k_val = sig["k"]
    if k_val < 20:
        kdj = "KDJ超卖"
    elif k_val > 80:
        kdj = "KDJ超买"
    else:
        kdj = f"KDJ中性({k_val:.0f})"
    signals.append(kdj)

    buy_count = sum(1 for s in signals if "上方" in s or "多头" in s or "超卖" in s)
    sell_count = sum(1 for s in signals if "下方" in s or "空头" in s or "超买" in s)
    if buy_count >= 2:
        verdict = "📈 建议关注"
    elif sell_count >= 2:
        verdict = "📉 谨慎观望"
    else:
        verdict = "➡️ 中性"
    return " | ".join(signals) + f" | {verdict}"


def main():
    parser = argparse.ArgumentParser(description="A股实时行情监控")
    parser.add_argument(
        "stocks", nargs="*", default=["600789", "000001", "600519", "601318", "000333"], help="股票代码列表"
    )
    parser.add_argument("--signals", action="store_true", help="显示策略信号")
    args = parser.parse_args()

    data = fetch_realtime(args.stocks)
    print(f"\n📊 A股实时行情 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'代码':<8} {'名称':<10} {'现价':<9} {'涨跌幅':<8} {'最高':<8} {'最低':<8}")
    print("-" * 55)
    for r in data:
        sign = "+" if r["pct"] >= 0 else ""
        print(
            f"{r['code']:<8} {r['name']:<10} {r['price']:<9.2f} {sign}{r['pct']:<7.2f}% {r['high']:<8.2f} {r['low']:<8.2f}"
        )

    if args.signals:
        print("\n📈 策略信号分析")
        print(f"{'代码':<8} {'名称':<10} {'现价':<9} {'信号'}")
        print("-" * 70)
        for r in data:
            sig = compute_signals(r["code"], r["price"])
            print(f"{r['code']:<8} {r['name']:<10} {r['price']:<9.2f} {sig}")


if __name__ == "__main__":
    main()
