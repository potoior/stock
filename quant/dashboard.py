import gradio as gr
import json
from datetime import datetime
from pathlib import Path
from data_fetcher import get_daily_data, fetch_realtime

DB_PATH = Path(__file__).parent / "stock_cache.db"

def compute_signals(code):
    try:
        df = get_daily_data(code, "20240101")
        if len(df) < 60:
            return {}
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        price = close.iloc[-1]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_val = 2 * (dif.iloc[-1] - dea.iloc[-1])
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        return {
            "price": round(price, 2), "ma5": round(ma5, 2),
            "ma10": round(ma10, 2), "ma20": round(ma20, 2),
            "macd": round(macd_val, 3), "macd_bull": dif.iloc[-1] > dea.iloc[-1],
            "k": round(k.iloc[-1], 1), "d": round(d.iloc[-1], 1),
            "kdj_signal": "超卖" if k.iloc[-1] < 20 else ("超买" if k.iloc[-1] > 80 else "中性"),
        }
    except:
        return {}

def create_portfolio_table():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT DISTINCT code FROM daily ORDER BY code").fetchall()
    conn.close()
    data = []
    codes = [r[0] for r in rows[:20]]
    quotes = fetch_realtime(codes)
    for q in quotes:
        sig = compute_signals(q["code"])
        trend = "↗" if q["pct"] > 0 else ("↘" if q["pct"] < 0 else "→")
        data.append([q["code"], q["name"], q["price"], f"{trend} {q['pct']:+.2f}%",
                     sig.get("ma5", "-"), sig.get("macd", "-"), sig.get("kdj_signal", "-")])
    return data

def refresh():
    data = create_portfolio_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data, f"最后更新: {now}"

columns = ["代码", "名称", "现价", "涨跌幅", "MA5", "MACD", "KDJ"]

with gr.Blocks(theme=gr.themes.Soft(
    primary_hue=gr.themes.colors.gray,
    neutral_hue=gr.themes.colors.gray,
    font=gr.themes.GoogleFont("Inter"),
), css="""
    .header { text-align: center; padding: 20px 0; }
    .header h1 { font-size: 28px; font-weight: 600; color: #1a1a1a; margin: 0; }
    .header p { font-size: 14px; color: #666; margin: 5px 0 0; }
    .status-bar { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; }
    .up { color: #d32f2f; font-weight: 500; }
    .down { color: #2e7d32; font-weight: 500; }
    .flat { color: #666; }
    footer { text-align: center; padding: 20px; color: #999; font-size: 12px; }
""") as demo:
    gr.HTML("""
    <div class="header">
        <h1>📊 A股量化监控</h1>
        <p>基于《半小时漫画股票实战法》策略知识库</p>
    </div>
    """)

    with gr.Row():
        status = gr.Markdown("最后更新: --", elem_classes="status-bar")
        refresh_btn = gr.Button("🔄 刷新数据", variant="secondary", size="sm")

    table = gr.Dataframe(
        headers=columns,
        datatype=["str", "str", "number", "str", "number", "str", "str"],
        row_count=20,
        col_count=(7, "fixed"),
        wrap=True,
    )

    refresh_btn.click(fn=refresh, outputs=[table, status])

    demo.load(fn=refresh, outputs=[table, status])

    gr.HTML("""
    <footer>
        ⚠️ 本系统仅供学习参考，不构成投资建议。<br>
        数据来源：新浪财经 | 回测框架：Backtrader
    </footer>
    """)

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False)