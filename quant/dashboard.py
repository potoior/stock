import sqlite3
from datetime import datetime
from pathlib import Path

import gradio as gr

import strategy_engine as se
from data_fetcher import fetch_realtime

DB_PATH = Path(__file__).parent / "stock_cache.db"


def compute_signals(code):
    return se.compute_basic_signals(code)


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
        data.append(
            [
                q["code"],
                q["name"],
                q["price"],
                f"{trend} {q['pct']:+.2f}%",
                sig.get("ma5", "-"),
                sig.get("macd", "-"),
                sig.get("kdj_signal", "-"),
            ]
        )
    return data


def refresh():
    data = create_portfolio_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return data, f"最后更新: {now}"


columns = ["代码", "名称", "现价", "涨跌幅", "MA5", "MACD", "KDJ"]

with gr.Blocks(
    theme=gr.themes.Soft(
        primary_hue=gr.themes.colors.gray,
        neutral_hue=gr.themes.colors.gray,
        font=gr.themes.GoogleFont("Inter"),
    ),
    css="""
    .header { text-align: center; padding: 20px 0; }
    .header h1 { font-size: 28px; font-weight: 600; color: #1a1a1a; margin: 0; }
    .header p { font-size: 14px; color: #666; margin: 5px 0 0; }
    .status-bar { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; }
    .up { color: #d32f2f; font-weight: 500; }
    .down { color: #2e7d32; font-weight: 500; }
    .flat { color: #666; }
    footer { text-align: center; padding: 20px; color: #999; font-size: 12px; }
""",
) as demo:
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
