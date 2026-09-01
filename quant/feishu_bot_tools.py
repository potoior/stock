"""飞书 Bot 工具 schema + SYSTEM_PROMPT(从 feishu_bot.py 提取,纯数据无函数)。

包含:
- TOOLS: 31 个 OpenAI function calling 工具 schema
- SYSTEM_PROMPT: Agent 系统提示词

被 feishu_bot.py 导入,通过 re-export 保持 `feishu_bot.TOOLS` / `feishu_bot.SYSTEM_PROMPT` 兼容。
"""

TOOLS = [
    # ---------- 个股/市场/选股/持仓 ----------
    {
        "type": "function",
        "function": {
            "name": "analyze_stock",
            "description": "对指定A股代码做技术面分析,返回综合判断+买入/卖出信号(基于当前已启用的策略)。用户问'分析X/看看X/X怎样'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "6位A股代码,如600519(茅台)、000001(平安银行)、300750(宁德时代)"
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_status",
            "description": "获取今日A股全市场概况:涨跌停分布、成交额、市场情绪。用户问'市场/大盘/行情/今天怎样'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_yujie_picks",
            "description": "获取今日玉姐精选 Top10 候选股(多因子评分排行,含命中规则)。用户问'玉姐/候选/精选/top/选股'时调用。支持按最低评分和命中规则过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_score": {
                        "type": "number",
                        "description": "最低评分门槛,只返回≥此分的股票。如 7=只看7+分强势股,5=玉姐精选默认门槛。不传=不过滤",
                        "default": 0
                    },
                    "hit_rule": {
                        "type": "string",
                        "description": "按命中规则过滤,只返回命中该规则的股票。规则名(中文)如 'MACD金叉'/'突破+金叉'/'RSI金叉'/'多线多头'/'深回撤'/'MOS低点'。不传=不过滤"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "查询当前模拟盘持仓:股票代码、数量、成本、买入日期。用户问'持仓/仓位/portfolio/股票池'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_finance",
            "description": "获取个股财务数据:PE/PB/总市值/流通市值/ROE/毛利率/净利率/EPS/营收/净利润/同比/资产负债率。用户问'财务/基本面/估值/PE/ROE/市值'时调用。支持中文简称(茅台)或6位代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "6位A股代码(600519)或中文简称(茅台)"
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_stocks",
            "description": "多股票对比:一次给 N 只股票(最多8只)的对比表(PE/PB/总市值/ROE/净利率/财报期)。用户问'对比X和Y'/'X和Y哪个好'/'比较几只股票'时调用,支持中文简称批量。用户一次贴出多只股票(名称或代码混合列表)问怎么看/分析时,优先用本工具(超8只取前8并说明)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "股票代码或名称列表,如 ['600519','000858'] 或 ['茅台','五粮液'],最多8只"
                    }
                },
                "required": ["codes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_sector",
            "description": "板块分析:展开板块成分股(8只代表股)批量对比 PE/PB/ROE/市值/净利率。已知板块:白酒/银行/医药/新能源/半导体/消费/军工/地产/电力/有色。用户问'分析白酒板块'/'看下银行板块'/'医药板块怎样'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {
                        "type": "string",
                        "description": "板块名(中文),如'白酒'/'银行'/'医药'/'新能源'/'半导体'"
                    }
                },
                "required": ["sector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_history_picks",
            "description": "查询过去某天的玉姐精选(历史复盘):列出那天的 Top10 + 评分分布。用户问'昨天的玉姐'/'前天玉姐精选'/'20260818的玉姐'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "日期,支持 '20260819' / '2026-08-19' / '昨天' / '前天' / '大前天'"
                    }
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_watchlist",
            "description": "管理自选股列表(按用户隔离)。用户说'加自选X'/'删自选X'/'我的自选'/'分析我的自选'/'群加自选'/'群自选'时调用。可批量加多只,analyze 批量对比自选股财务,group_* 群共享池(全群可见)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list", "analyze",
                                 "group_add", "group_remove", "group_list", "group_analyze"],
                        "description": "add/remove/list/analyze=个人自选;group_*=群共享池(全群可见)"
                    },
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "股票代码或名称列表,如 [\"600519\"] 或 [\"茅台\",\"五粮液\"]。list/analyze/group_list/group_analyze 可省略"
                    }
                },
                "required": ["action"]
            }
        }
    },
    # ---------- 策略管理 skill ----------
    {
        "type": "function",
        "function": {
            "name": "list_strategies",
            "description": "列出所有策略(54个内置+自定义),含开关状态、当前参数、回测超额收益。用户问'有哪些策略/策略状态/策略列表/哪些策略开了'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_strategy_library",
            "description": "查策略大全4来源(漫画书/操练大全/玉姐精选/AI)中的策略,支持多维度过滤。用户问'策略大全/操练大全/漫画书策略/某书第X章/某策略在哪些书里/某书里哪些没实现'时调用。注意:本工具只查策略文档;用户要按技术条件找股票时用 scan_with_strategy 或 scan_with_yujie,不要用本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["book_cartoon", "book_caolian", "yujie_custom", "ai_custom", ""],
                        "description": "来源过滤,空字符串=全部。book_cartoon=半小时漫画股票实战法、book_caolian=中国股市操练大全、yujie_custom=玉姐精选、ai_custom=AI自定义"
                    },
                    "category": {
                        "type": "string",
                        "description": "章节/分类名模糊匹配(子串包含),空字符串=不过滤。合法分类: 技术指标/均线系统/量能策略/实战战法/组合策略/第8章/第12章/第14章/第15章/第16章/第17章/第20章/评分规则/自定义。不要猜'低位/热门/突破'这类词,它们不是分类名"
                    },
                    "implemented_only": {
                        "type": "boolean",
                        "description": "true=只看已实现,false=只看未实现,不传=全部"
                    },
                    "include_meta": {
                        "type": "boolean",
                        "description": "true=附带书的元数据(作者/简介/章节数/文件列表)"
                    },
                    "cross_ref": {
                        "type": "string",
                        "description": "跨来源对比:传策略id(如macd),返回该策略在哪些来源/章节出现。优先级高于其他过滤参数"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_yujie_detail",
            "description": "查询玉姐精选的详细信息:10条评分规则、每条score权重、回测表现。用户问'玉姐评分规则/玉姐怎么打分/玉姐回测表现'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_with_strategy",
            "description": "用指定策略单独分析个股(只看该策略的信号,不跑全部54个)。用户问'看X的MACD/KDJ/BOLL信号'、'用某策略分析X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位A股代码"},
                    "strategy_id": {
                        "type": "string",
                        "description": "策略id,如 macd/kdj/boll/rsi/dmi/bottom/top/zt 等"
                    }
                },
                "required": ["code", "strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_with_yujie",
            "description": "用玉姐精选10条评分规则分析个股,给出综合评分+命中规则+未命中规则+解读。用户问'用玉姐分析X/玉姐评分看X/玉姐策略测X'时调用。与 analyze_with_strategy 不同:玉姐是复合评分体系(10条规则累加),不是单策略买卖信号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位A股代码"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_strategy",
            "description": "开启或关闭某策略(修改 config.json,影响后续 analyze 的信号)。用户明确说'关闭X策略/打开X策略/启用X'时调用。操作类,LLM 需确认用户意图明确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id,如 macd/kdj/ma_combo"},
                    "enabled": {"type": "boolean", "description": "true=开启,false=关闭"}
                },
                "required": ["strategy_id", "enabled"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_strategy_params",
            "description": "调整策略参数(修改 config.json,影响后续 analyze)。用户明确说'把BOLL周期改成X/MACD signal改成X'时调用。操作类,LLM 需确认用户意图明确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id"},
                    "params": {
                        "type": "object",
                        "description": "参数键值对,如 {\"period\": 30, \"std\": 2.5}"
                    }
                },
                "required": ["strategy_id", "params"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enable_library_strategy",
            "description": "从策略大全引入一个未实现的策略(标记为已实现并启用)。仅对 strategy_library.json 中 implemented=false 的策略有意义。用户说'引入X策略/启用大全里的X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "library_id": {"type": "string", "description": "策略大全中的策略id,如 bottom_kline/top_volume"}
                },
                "required": ["library_id"]
            }
        }
    },
    # ---------- 回测/寻优 skill(耗时操作) ----------
    {
        "type": "function",
        "function": {
            "name": "backtest_strategy",
            "description": "对指定策略做全市场回测,返回超额 alpha 收益(相对基准)。耗时约 1-2 分钟,会先返回'开始回测'提示。用户问'回测X策略/X策略表现怎样/X策略历史收益'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id(必须是内置策略)"},
                    "sample": {"type": "integer", "description": "抽样股票数,默认 0=全市场约4700只。调试可用 200", "default": 0}
                },
                "required": ["strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grid_search_strategy",
            "description": "对指定策略做参数网格寻优,返回最优参数组合。耗时约 2-5 分钟。仅支持 macd/kdj/boll/dmi 四策略。用户问'寻优X策略/X策略最优参数/调参X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "enum": ["macd", "kdj", "boll", "dmi"], "description": "策略id"},
                    "sample": {"type": "integer", "description": "抽样股票数,默认 400", "default": 400}
                },
                "required": ["strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "combo_backtest",
            "description": "多策略组合回测:AND=所有策略同日同时触发买入信号,OR=任一策略触发。对比组合 vs 各策略单独的超额收益。耗时 1-10 分钟(sample=400 约 1-3 分钟,全市场 5-10 分钟)。用户问'组合回测X和Y/X+Y策略组合/X和Y同时触发效果'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "策略 id 列表(至少2个),如 [\"macd\",\"boll\"]"},
                    "mode": {"type": "string", "enum": ["and", "or"],
                             "description": "and=所有策略同日触发, or=任一触发。默认 and", "default": "and"},
                    "horizon": {"type": "integer", "description": "持有期(天),默认 20", "default": 20},
                    "sample": {"type": "integer", "description": "抽样股票数,默认 400", "default": 400}
                },
                "required": ["strategy_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scan_with_strategy",
            "description": "全市场扫描指定策略,返回当日触发 buy 信号的股票列表(选股)。耗时约 5-30 分钟(全市场约4700只)。用户问'用X策略选股/哪些股票今天触发X信号/X策略选股/X策略选哪些'时调用。注意:与 analyze_with_strategy(判断个股) 不同,本工具是反向操作(给定策略找股票)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id,如 macd/bottom/dragon_pullback/pe_select 等内置策略"},
                    "top_n": {"type": "integer", "description": "返回前 N 只(按涨幅降序),默认 20", "default": 20},
                    "min_amount_yi": {"type": "number", "description": "最小成交额(亿)过滤,默认 0.5", "default": 0.5},
                    "limit": {"type": "integer", "description": "限制扫描股票数(调试用),默认 0=全市场", "default": 0}
                },
                "required": ["strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_news",
            "description": "查询个股相关新闻(东财搜索接口,实时抓取)。用户问'X股票有什么新闻/X最近消息/X公司动态/跟X相关的新闻'时调用。返回最近 N 条提到该股票名或代码的新闻(已过滤无关列表新闻)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码或股票名,如 301189 / 茅台 / 600519"},
                    "num": {"type": "integer", "description": "返回新闻条数,默认 15", "default": 15}
                },
                "required": ["code"]
            }
        }
    },
    # ---------- 市场数据(新) ----------
    {
        "type": "function",
        "function": {
            "name": "get_lhb",
            "description": "查询龙虎榜数据(当日上榜个股 + 机构/游资席位净买卖)。用户问'龙虎榜/上榜个股/今日哪些股票上榜'时调用。支持指定日期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期(可选),如 '20260820' / '2026-08-20' / '昨天',不传=最新"},
                    "top_n": {"type": "integer", "description": "返回条数,默认 20", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_north_flow",
            "description": "查询北向资金(沪深股通)净流入。用户问'北向资金/外资流入/沪深股通/北向今天怎样'时调用。返回近 N 日沪股通+深股通+合计净流入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "近 N 日,默认 5", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_main_flow",
            "description": "查询个股主力资金流(超大单/大单/中单/小单净流入)。用户问'X主力资金/X资金流/X资金流入/X主力动向'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码或股票名,如 600519 / 茅台"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_concept_sectors",
            "description": "概念板块反查:给定股票反查它属于哪些行业/概念板块。用户问'X属于什么板块/X是哪个板块的/X有哪些概念'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位股票代码或股票名,如 600519 / 茅台"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_index",
            "description": "查询指数行情(上证/深成/创业板/科创50/北证50)。用户问'上证/大盘指数/创业板/科创50/北证50/沪深300'时调用。不传 name 返回全部主要指数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "指数名(可选),如 '上证'/'深成'/'创业板'/'科创50'/'北证50'/'沪深300'。不传=全部主要指数", "default": ""}
                }
            }
        }
    },
    # ---------- 玉姐全市场实时扫描 ----------
    {
        "type": "function",
        "function": {
            "name": "scan_with_yujie",
            "description": "全市场玉姐评分实时扫描(耗时1-3分钟)。用 daily 表已缓存数据对全市场4700+只股票重新打分,返回 Top N 高分股。用户说'扫描整个市场/全市场玉姐/实时玉姐评分/重新扫一遍/按玉姐选股'时调用。与 get_yujie_picks(盘前09:35扫描结果)区别:这是实时重跑全市场评分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "description": "返回前 N 只(按评分降序),默认 20", "default": 20},
                    "min_score": {"type": "number", "description": "最低评分门槛,默认 5.0(玉姐精选默认)。降低到 3 可看更多弱势候选", "default": 5.0},
                    "limit": {"type": "integer", "description": "限制扫描股票数(调试用),0=全市场", "default": 0}
                }
            }
        }
    },
    # ---------- 板块资金流 ----------
    {
        "type": "function",
        "function": {
            "name": "get_sector_flow",
            "description": "查行业/概念板块主力资金流排名(按主力净流入降序)。用户问'哪个板块资金流入最多/行业资金流/概念板块资金/主力净流入板块'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector_type": {"type": "string", "enum": ["industry", "concept"],
                                    "description": "板块类型: industry=行业板块(有色金属/电子/通信...), concept=概念板块(融资融券/5G/MSCI...)。默认 industry", "default": "industry"},
                    "top_n": {"type": "integer", "description": "返回前 N 个板块,默认 10", "default": 10}
                }
            }
        }
    },
    # ---------- 市场情绪速览 ----------
    {
        "type": "function",
        "function": {
            "name": "get_market_sentiment",
            "description": "市场情绪速览:5大指数涨跌 + 行业板块资金流前5 + 概念板块资金流前5。用户问'市场情绪/资金面/今天什么板块强/市场速览'时调用。比 get_index 多了板块资金流维度。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    # ---------- 条件选股 ----------
    {
        "type": "function",
        "function": {
            "name": "screen_stocks",
            "description": "条件选股:按 PE/PB/市值范围筛选全市场股票。耗时约 5-15 秒。用户问'找出PE<20的/低估值股/PE<20且PB<3的股票/大盘股有哪些'时调用。注意:只支持 PE/PB/市值,不支持 ROE(财报数据需另查)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pe_max": {"type": "number", "description": "PE_TTM 上限,如 20=只看 PE≤20。负值(亏损)自动过滤", "default": None},
                    "pe_min": {"type": "number", "description": "PE_TTM 下限", "default": None},
                    "pb_max": {"type": "number", "description": "PB 上限", "default": None},
                    "pb_min": {"type": "number", "description": "PB 下限", "default": None},
                    "mv_min_yi": {"type": "number", "description": "总市值下限(亿元),如 100=只看市值≥100亿", "default": None},
                    "mv_max_yi": {"type": "number", "description": "总市值上限(亿元)", "default": None},
                    "top_n": {"type": "integer", "description": "返回前 N 只,默认 20", "default": 20},
                    "sort_by": {"type": "string", "enum": ["pe", "pb", "mv"],
                                "description": "排序: pe=PE升序(低估值优先), pb=PB降序, mv=市值降序。默认 pe", "default": "pe"}
                }
            }
        }
    },
]
SYSTEM_PROMPT = """你是 A 股量化分析助手(飞书群聊 Bot),有 31 个工具。回复严格 ≤400 字,markdown 格式,只给关键结论+数字,加风险提示。

【数据查询】
- analyze_stock(code): 个股技术面分析+K线图。code 支持中文简称/拼音/6位代码
- get_market_status(): 今日市场概况(涨跌停/成交额)
- get_yujie_picks(min_score?, hit_rule?): 今日玉姐精选(盘前09:35结果,默认Top10+图)
- get_portfolio(): 模拟盘持仓
- get_finance(code): 财务数据(PE/PB/市值/ROE/毛利率/EPS/营收/净利润)
- compare_stocks(codes): 多股对比(最多8只),PE/PB/ROE对比表
- analyze_sector(sector): 板块分析(白酒/银行/医药/新能源/半导体/消费/军工/地产/电力/有色)
- query_history_picks(date): 历史玉姐复盘(date支持'昨天'/'前天'/'20260818')
- manage_watchlist(action, codes?): 自选股管理(action=add/remove/list/analyze/group_*,按用户隔离)
  · analyze: 批量分析自选股(PE/PB/ROE 对比),最多 8 只
  · group_add/group_list/group_remove/group_analyze: 群共享自选池(全群可见)

【策略管理】
- list_strategies(): 列出所有策略+开关+参数
- get_strategy_library(source?, category?, implemented_only?, include_meta?, cross_ref?): 策略大全查询
  · source: book_cartoon/book_caolian/yujie_custom/ai_custom
  · cross_ref: 跨来源查策略在哪些书出现(传策略id)
- get_yujie_detail(): 玉姐10条评分规则+权重+回测表现
- analyze_with_strategy(code, strategy_id): 用指定策略分析个股+K线图
  · strategy_id 支持引擎id(macd)/大全id(macd_8)/中文名(抄底)模糊匹配
- analyze_with_yujie(code): 玉姐10条评分规则分析个股+玉姐专属图
- toggle_strategy(strategy_id, enabled): 开关策略(操作类,需用户明确意图)
- set_strategy_params(strategy_id, params): 调参数(操作类)
- enable_library_strategy(library_id): 从大全引入策略(操作类)

【回测寻优(耗时1-5分钟)】
- backtest_strategy(strategy_id, sample?): 全市场回测
- grid_search_strategy(strategy_id, sample?): 参数网格寻优(仅macd/kdj/boll/dmi)
- combo_backtest(strategy_ids, mode?, horizon?, sample?): 多策略组合回测(AND/OR),耗时1-3分钟
  · mode: and=同日同时触发, or=任一触发
  · 示例: "组合回测MACD和BOLL" / "MACD+KDJ同时触发效果"
- scan_with_strategy(strategy_id, top_n?, min_amount_yi?, limit?): 全市场扫描某策略选股(5-30分钟)
- scan_with_yujie(top_n?, min_score?, limit?): 全市场玉姐评分实时扫描(1-3分钟)
  · 与 get_yujie_picks 区别: 这是实时重跑全市场评分,不是盘前缓存

【新闻资讯】
- get_stock_news(code, num?): 个股新闻(东财搜索,strict过滤无关列表新闻)

【市场数据(实时)】
- get_lhb(date?, top_n?): 龙虎榜
- get_north_flow(days?): 北向资金(沪股通+深股通+合计)
- get_main_flow(code): 个股主力资金流(超大/大/中/小单)
- get_concept_sectors(code): 概念板块反查(给定股票反查所属板块)
- get_index(name?): 指数行情(上证/深成/创业板/科创50/北证50)
- get_sector_flow(sector_type?, top_n?): 行业/概念板块主力资金流排名
  · sector_type: industry(默认)/concept
- get_market_sentiment(): 市场情绪速览(5大指数+行业/概念板块资金流Top5)
- screen_stocks(pe_max?, pb_max?, mv_min_yi?, ...): 条件选股(PE/PB/市值筛选)
  · 用户问"找出PE<20的/低估值股/大盘股"时调用,耗时 5-15 秒
  · 只支持 PE/PB/市值,不支持 ROE

【工作流程】
1. 需要数据时根据问题决定调用哪个工具(可多次/组合调用);闲聊/抱怨/确认类消息可不用工具直接自然回复
2. 拿到原始数据后用简洁中文解释,只给关键结论+数字
3. 回复 ≤400 字,markdown,不要复述全部数据,超5行政用一句话总结
4. 不编造数据,只基于工具返回事实
5. 涉及投资判断加风险提示

【多步任务规划(GOAP)】
- 需调用≥2工具的复杂任务,在第一个 content 字段用1-2句简述:目标+计划调用哪些工具
- 思考文字不展示给用户,只作上下文
- 单步任务(如"分析茅台")无需规划,直接调工具

【工具纪律】
- 参数严格按 schema(code必须是6位字符串"600519",不能传"茅台")
- 工具返回错误时阅读错误信息修正参数重试,不要重复同样错误
- 工具结果可能被截断(超3000字),关键信息在前段

【跨轮上下文(重要)】
- 系统保留最近6轮对话,用户问题可能承接上文
- "五粮液呢"→用同方法分析五粮液;"KDJ呢"→看KDJ策略;"11-20呢"→玉姐11-20名
- 收到指代不明的简短问题,结合历史理解,不要要求用户重述
- 用户说"重置/新话题/忘了吧/清空"系统自动清空历史

【操作类授权】
- 用户明确意图(如"关闭X策略"、"BOLL周期改成30")才调用
- 模糊请求(如"看看策略")只调查询类

【意图判别(重要,避免答非所问)】
- 查文档: "策略有哪些/是什么/详情/在哪些书里" → get_strategy_library
- 按策略找股票: "按X策略找/用X策略选股/哪些股票触发X" → scan_with_strategy
- 用户贴多只股票(≥2只)问怎么看 → compare_stocks(超8只取前8并说明)
- 描述选股条件(低位/多头/反转/热门/技术形态) → scan_with_yujie 或 scan_with_strategy
- 承接词("可以的/继续/嗯") → 结合上文继续任务;无处可续则自然回复,不调工具
- 抱怨/闲聊("怎么不说话"等) → 自然回复,不调工具
- 看不懂意图时 → 简短反问确认,禁止乱猜工具

【模式识别】
- "漫画书/操练大全/玉姐/AI" → source 过滤(仅限查策略文档;若用户要找股票见上)
- "第X章/抄底/逃顶" → category 匹配
- "已实现/未实现" → implemented_only
- "MACD在哪些书里" → cross_ref
- "扫描整个市场/全市场玉姐/重新扫" → scan_with_yujie(不是get_yujie_picks)
- "哪个板块资金流入最多" → get_sector_flow
- "市场情绪/资金面" → get_market_sentiment
"""
