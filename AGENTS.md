# AGENTS.md - A 股量化系统项目约定

> 给协作 Agent / 未来自己 / 新接手者看的快速上手文档。

## 项目概览

A 股量化分析系统,集成飞书群聊 Bot(Function Calling ReAct Agent),覆盖:
- 数据获取(腾讯/新浪接口,日 K 线 + 实时价)
- 45 内置策略信号引擎(MACD/KDJ/BOLL/RSI/玉姐 10 条规则 + 操练大全12/14/15/16/17/20章 + 漫画书量能/实战战法等)
- 玉姐精选全市场扫描(多因子评分排行)
- 回测 + 参数网格寻优
- 飞书 Bot Agent(31 个 skill,跨轮记忆,自选股,群共享自选股,财务数据,板块分析,历史复盘,策略选股,个股新闻,龙虎榜,北向资金,主力资金流,板块反查,指数行情,板块资金流,市场情绪,多策略组合回测,条件选股)
- 策略大全(4 来源 73 策略:漫画书 29 + 操练大全 32 + 玉姐 10 + AI 2,已实现 72 个)

## 目录结构

```
quant/
├── feishu_bot.py          # 飞书长连接 Agent(~3480 行,29 skill)
├── feishu_image.py        # matplotlib 图表(K 线/玉姐/回测/市场)
├── feishu.py              # 飞书 webhook 推送(日报/告警)
├── stock_names.py         # 股票名称解析(腾讯 smartbox + sqlite 缓存)
├── stock_finance.py       # 财务数据(东方财富双接口,PE/PB/ROE/市值/EPS,sqlite 缓存 1 天)+ 股东人数(datacenter)
├── stock_market_extras.py # 市场扩展数据(龙虎榜/北向资金/主力资金流/概念板块反查/指数行情)
├── strategy_engine.py     # 54 策略信号引擎(23 原有 + 22 新增 + 5 补齐 + 4 经典形态:K线/顶背离/缺口)
├── backtest_builtin.py    # 回测引擎(workers=1,非线程安全)
├── yujie_scan.py          # 玉姐精选评分(10 条规则)
├── daily_scan.py          # 每日 09:25 盘前日报 + 15:05 盘后复盘 + 飞书推送(systemd timer)
├── news_monitor.py        # 自选股新闻监控(群共享池,11:45/20:30 推送新消息)
├── news_reasoning.py      # 新闻掘金·因果推理(新闻→概念落地→成分股→因果链,21:30 推送)
├── watchlist_check.py     # 持仓与自选每日体检(盈亏+卖出信号,15:30 推送)
├── strategy_library.json  # 策略大全(4 来源 73 策略)
├── data_fetcher.py        # 数据获取(腾讯/新浪)
├── api.py                 # FastAPI 服务(/api/* 端点)
├── dashboard.py           # Gradio 看板
├── ai_decider.py          # AI 决策器(本地网关 LLM)
├── config.json            # 配置(.gitignore,含飞书 app_id/secret)
├── .env                   # 环境变量(.gitignore,AI_API_KEY 等)
├── tests/                 # pytest 测试
│   ├── test_feishu_bot.py     # 63 用例(历史/分段/重置/自选股/校验/锁/对比/板块/复盘/日志/压缩)
│   ├── test_feishu.py        # 16 用例(飞书推送)
│   └── ...
└── reports/               # 日报输出(daily_YYYYMMDD.md)
```

## 运行环境

- **OS**: Ubuntu 24.04,Python 3.12.3(本地) / 3.11(CI)
- **venv**: `quant/.venv/bin/python`
- **部署**: systemd user service,不用 Docker
- **API 端口**: 18000(`PORT` 环境变量,`HOST=0.0.0.0`)
- **AI 网关**: `http://10.10.250.219/v1/chat/completions`,模型 `DSV4-Flash-0731-1M`
- **飞书长连接**: `wss://msg-frontier.feishu.cn/ws/v2`,lark-oapi WsClient,auto_reconnect=True

## 常用命令

```bash
# 跑测试(必须加 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 绕过 ROS launch_testing 冲突)
cd quant && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest

# Lint
.venv/bin/ruff check .

# 启动 API 服务
systemctl --user start quant-api.service

# 启动/重启飞书 Bot
systemctl --user restart feishu-bot.service
systemctl --user status feishu-bot.service
journalctl --user -u feishu-bot -f

# daily-scan 定时器(盘前日报,每个交易日 09:25 自动跑)
systemctl --user list-timers daily-scan.timer
systemctl --user start daily-scan.service   # 立即手动触发一次
journalctl --user -u daily-scan -f

# 盘后复盘(daily-afterclose.timer,每个交易日 15:05)与自选股新闻监控(news-monitor.timer,11:45/20:30)
systemctl --user list-timers daily-afterclose.timer news-monitor.timer

# CLI 测试 Agent(不走飞书)
.venv/bin/python feishu_bot.py --agent "分析茅台"

# 单次跑日报(同 daily-scan.service 但不通过 systemd)
.venv/bin/python daily_scan.py
```

## 关键约定

### 部署
- **systemd user service**,配置在 `~/.config/systemd/user/feishu-bot.service` 和 `quant-api.service`
- 都 `enabled`,Restart=on-failure
- **daily-scan**: `daily-scan.timer`(Mon-Fri 09:25 CST) 触发 `daily-scan.service`(oneshot),日志写 `/tmp/daily_scan.log`
- **daily-afterclose**: `daily-afterclose.timer`(Mon-Fri 15:05) 盘后复盘,日志同 daily-scan
- **news-monitor**: `news-monitor.timer`(Mon-Fri 11:45/20:30) 群共享自选池新闻监控推送,日志写 `/tmp/news_monitor.log`
- **news-reasoning**: `news-reasoning.timer`(Mon-Fri 21:30) 新闻掘金·因果推理推送,日志写 `/tmp/news_reasoning.log`
- **watchlist-check**: `watchlist-check.timer`(Mon-Fri 15:30) 持仓与自选股体检推送,日志写 `/tmp/watchlist_check.log`
- 配置文件 `config.json` 和 `.env` 在 `.gitignore` 中,**不要提交**

### 测试
- 必须用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`(系统装了 ROS launch_testing,会冲突)
- 测试不联网,外部依赖 mock 或用临时 db(`tmp_path` fixture)
- backtest_builtin 多线程非线程安全,测试用 `workers=1`

### Agent 设计
- **Function Calling ReAct**: LLM 自主决策调工具,失败降级到 `route()` 关键词路由
- **31 个 skill**: 4 数据查询 + 5 策略查询 + 3 策略操作 + 4 回测寻优(含组合回测) + 1 自选股(含群共享/批量分析) + 1 财务 + 3 批量(对比/板块/历史复盘) + 1 个股新闻 + 7 市场数据(龙虎榜/北向/主力资金流/板块反查/指数/板块资金流/市场情绪) + 1 条件选股 + 1 条件选股
  - 数据查询: `analyze_stock` / `get_market_status` / `get_yujie_picks` / `get_portfolio`
  - 策略查询: `list_strategies` / `get_strategy_library` / `get_yujie_detail` / `analyze_with_strategy` / `analyze_with_yujie`
  - 策略操作: `toggle_strategy` / `set_strategy_params` / `enable_library_strategy`
  - 回测寻优选股: `backtest_strategy` / `grid_search_strategy` / `scan_with_strategy`(全市场策略选股) / `scan_combo`(多策略组合选股,AND共振/OR宽松,耗时5-30分钟)
  - 自选股: `watchlist`(add/remove/list)
  - 财务: `get_finance`(单股 PE/PB/市值/ROE/毛利率/净利率/EPS/营收/净利润)
  - 批量: `compare_stocks`(多股对比) / `analyze_sector`(板块成分股) / `query_history_picks`(历史玉姐复盘)
  - 新闻: `get_stock_news`(个股新闻,东财搜索接口,strict 过滤无关列表新闻)
  - 市场数据: `get_lhb`(龙虎榜) / `get_north_flow`(北向资金) / `get_main_flow`(主力资金流) / `get_concept_sectors`(板块反查) / `get_index`(指数行情)
- **跨轮记忆**: `session_id = f"{chat_id}:{sender}"`,sqlite `agent_history.db`,最近 6 轮
  - assistant >500 字裁到 200 字 + 截断标记
  - >7 天自动过期(`_purge_old_history` 启动时清理)
  - **历史压缩**(Compaction): >=10 条触发 LLM 总结旧轮成 1 条摘要,保留最近 8 条原文,失败降级硬截断
- **重置命令**: 整句精确匹配(去标点),不用子串匹配(避免"重置 BOLL 参数"误判)
- **会话级并发锁**: 同 session_id 串行,防连发消息 race
- **参数 schema 预校验**: 对照 TOOLS schema,不合法直接回灌不执行
- **工具错误自愈**: 错误回灌"请用正确参数重试",LLM 自行修正
- **GOAP Scratchpad**: 复杂多步任务先写 Goal+Actions 规划
- **工具结果截断**: 超 3000 字截断,防上下文污染
- **图片缓存**: 同股同日 K 线图复用(_IMG_CACHE 30 张 LRU),玉姐图带 score 不缓存
- **LLM 重试**: 5xx/网络异常重试 1 次,4xx 不重试,失败给友好提示

### 操作类工具授权
- 用户明确表达意图(如"关闭 X 策略")才调用
- 模糊请求(如"看看策略")只调查询类
- LLM 智能判断,不需要硬编码规则

### 日志
- `RotatingFileHandler` `/tmp/feishu_bot.log` 5MB×3
- 同时输出控制台(journalctl 可查)
- 工具调用记 `Agent step N 调用 X(args)`
- **结构化审计日志**: `/tmp/feishu_bot_audit.jsonl`(JSONL,每行一次工具调用,含 ts/session_id/step/tool/args/result_size/duration_ms/error)

### 数据缓存
- `stock_cache.db`: 日 K 线(`daily` 表,~440 万行 405MB,正常)+ 玉姐精选 + AI 缓存 + 股票名(17 行)+ 财务数据
- `agent_history.db`: 对话历史
- `agent_watchlist.db`: 自选股(按 session_id 隔离)
- 所有 `*.db` 在 `.gitignore` 中

### 策略大全(strategy_library.json)
- 4 来源: `book_cartoon`(漫画书 29) + `book_caolian`(操练大全 32) + `yujie_custom`(玉姐 10) + `ai_custom`(AI 2)
- 73 策略中 72 已实现,1 保留未实现(T+0,需分钟数据,无法用日 K 线实现)
- 每个策略有 `id` / `name` / `category` / `implemented`(是否在引擎里实现) / `engine_id`(关联策略引擎中的实现 id)
- 已融入其他策略的标 `implemented=True` + `engine_id` 指向其融入策略(如 bottom_kline → bottom)
- `cross_ref` 跨来源查同一策略在哪些书里出现

### 内置策略列表(strategy_engine.py,54 个)
- 原有 23: macd/kdj/ma_stop/boll/dmi/psy/bias/sar/bbiboll/tower/ma_combo/two_line/life_line/three_third/sparrow/bounce/volume_div/resonance/dmi_psy/rsi/bottom/top/zt
- 12章 投资法则(4): trend_follow(顺势)/pyramid(金字塔)/stop_profit(暴利收手)/plan_trade(计划交易)
- 漫画书 量能/实战战法(5): high_volume(高量柱)/demon_stock(看妖股)/dragon_pullback(龙回头)/support_resistance(压力支撑)/range_trade(区间交易)
- 漫画书 实战战法 补齐(2): daban(打板,日K线简化版)/fupan(复盘法,量化版)
- 15章 抄底(2): bottom_ma(均线识底)/bottom_time(时间识底,斐波那契时间窗)
- 16章 逃顶(2): top_weekly(周线见顶)/top_monthly(月线见顶)
- 17章 跟庄(5): zhuang_test(试盘)/zhuang_build(建仓)/zhuang_pull(拉高)/zhuang_ship(出货)/zhuang_wash(洗盘)
- 20章 涨停细分(3): zt_type(类型)/zt_unsealed(封不牢)/zt_pull(拉高型)
- 14章 基本面(4): pe_select(市盈率)/roe_pe(ROE+PE 复合)/shareholder_select(股东人数变动,东财datacenter)/policy_select(政策选股,新闻关键词)
- 经典 K 线形态(4): kline_pattern(早晨/黄昏之星/锤头/流星/吞没/十字星/红三兵/黑三兵/孕线)/macd_top_divergence(MACD顶背离)/rsi_top_divergence(RSI顶背离)/gap(缺口识别)
- 板块热点 hotspot_select 在 daily_scan.scan_hotspot_stocks 实现(市场层面,非单股策略)
- 需联网的策略(shareholder_select/policy_select)不可用于 scan_with_strategy 全市场扫描,只能 analyze 个股

### 股票名识别(stock_names.py)
- 6 位代码直接返回
- 中文简称:腾讯 smartbox 搜索 + sqlite 缓存(1 天)
- 不预抓全市场,按需搜索
- 缓存全称 + 简称(去地名前缀)

### 财务数据(stock_finance.py)
- 东方财富双接口:push2 实时估值(PE/PB/市值)+ F10 财报(ROE/毛利率/净利率/EPS)
- sqlite 缓存 1 天(PE/市值每日变,财报不变)
- 字段: pe_ttm/pb/total_mv/float_mv/roe/gross_margin/net_margin/eps/revenue/net_profit/revenue_yoy/profit_yoy
- 股东人数变动: datacenter `RPT_F10_EH_HOLDERNUM` 接口(fetch_shareholder,不缓存),返回最近 2 期对比(change_pct/hold_focus)

## 已知问题

- `daily` 表 405MB(440 万行日 K 线): 设计如此,后续可加分区/归档
- lark-oapi ws 偶发 ping timeout: 已启用 auto_reconnect=True

## 不要做

- 不要用 python-dotenv(`ai_decider.load_env()` 手动解析 .env)
- 不要用 Docker(本地拉不到 python:3.11-slim,systemd 部署)
- 不要改 backtest_builtin 的 workers 默认值(非线程安全)
- 不要把 `config.json` / `.env` / `*.db` 提交到 git
- 不要在测试里联网(全部 mock 或 tmp_path)
