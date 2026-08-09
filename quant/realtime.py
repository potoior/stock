import urllib.request
import json
from datetime import datetime

def sina_symbol(code):
    code = code.upper().replace("SH", "").replace("SZ", "").replace(".", "")
    if code.startswith("6"): return "sh" + code
    elif code.startswith(("0", "3")): return "sz" + code
    elif code.startswith(("8", "4")): return "bj" + code
    return "sh" + code

def fetch_realtime(codes):
    symbols = [sina_symbol(c) for c in codes]
    url = "http://hq.sinajs.cn/list=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    results = []
    for i, code in enumerate(codes):
        line = data.split("\n")[i] if i < len(data.split("\n")) else ""
        if f"hq_str_{symbols[i]}" not in line:
            continue
        parts = line.split('"')[1].split(",") if '"' in line else []
        if len(parts) < 30:
            continue
        results.append({
            "code": code,
            "name": parts[0],
            "open": float(parts[1]),
            "yclose": float(parts[2]),
            "price": float(parts[3]),
            "high": float(parts[4]),
            "low": float(parts[5]),
            "volume": int(parts[8]) if parts[8] else 0,
            "amount": float(parts[9]) if parts[9] else 0,
            "change_pct": round((float(parts[3]) - float(parts[2])) / float(parts[2]) * 100, 2) if float(parts[2]) else 0,
        })
    return results

def fetch_minute_kline(code):
    symbol = sina_symbol(code)
    url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&datalen=120&scale=60&ma=no")
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/"})
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read().decode("gbk"))
    rows = []
    for item in data:
        rows.append({
            "time": item["day"],
            "open": float(item["open"]),
            "close": float(item["close"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "volume": float(item.get("volume", 0)),
        })
    return rows

def compute_signals(code, price):
    """基于历史K线计算策略买卖信号（简化版）"""
    from data_fetcher import get_daily_data
    import pandas as pd
    try:
        df = get_daily_data(code, "20240101")
        if len(df) < 60:
            return "数据不足"
        df = df.sort_values("date")
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        signals = []
        # 1. 5日均线
        ma5 = close.rolling(5).mean().iloc[-1]
        signals.append("MA5上方" if price > ma5 else "MA5下方")

        # 2. MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        golden_macd = dif.iloc[-1] > dea.iloc[-1]
        signals.append("MACD多头" if golden_macd else "MACD空头")

        # 3. KDJ
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        if k.iloc[-1] < 20:
            kdj = "KDJ超卖"
        elif k.iloc[-1] > 80:
            kdj = "KDJ超买"
        else:
            kdj = f"KDJ中性({k.iloc[-1]:.0f})"
        signals.append(kdj)

        # 综合建议
        buy_count = sum(1 for s in signals if "上方" in s or "多头" in s or "超卖" in s)
        sell_count = sum(1 for s in signals if "下方" in s or "空头" in s or "超买" in s)
        if buy_count >= 2:
            signal = "📈 建议关注"
        elif sell_count >= 2:
            signal = "📉 谨慎观望"
        else:
            signal = "➡️ 中性"
        return " | ".join(signals) + f" | {signal}"
    except Exception as e:
        return f"计算失败: {e}"

def main():
    import argparse
    parser = argparse.ArgumentParser(description="A股实时行情监控")
    parser.add_argument("stocks", nargs="*", default=["600789", "000001", "600519", "601318", "000333"],
                        help="股票代码列表")
    parser.add_argument("--signals", action="store_true", help="显示策略信号")
    args = parser.parse_args()

    data = fetch_realtime(args.stocks)
    print(f"\n📊 A股实时行情 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'代码':<8} {'名称':<10} {'现价':<9} {'涨跌幅':<8} {'最高':<8} {'最低':<8}")
    print("-" * 55)
    for r in data:
        sign = "+" if r["change_pct"] >= 0 else ""
        print(f"{r['code']:<8} {r['name']:<10} {r['price']:<9.2f} {sign}{r['change_pct']:<7.2f}% {r['high']:<8.2f} {r['low']:<8.2f}")

    if args.signals:
        print(f"\n📈 策略信号分析")
        print(f"{'代码':<8} {'名称':<10} {'现价':<9} {'信号'}")
        print("-" * 70)
        for r in data:
            sig = compute_signals(r["code"], r["price"])
            print(f"{r['code']:<8} {r['name']:<10} {r['price']:<9.2f} {sig}")

if __name__ == "__main__":
    main()