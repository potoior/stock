"""news_reasoning 单元测试:mock LLM/网络,不联网。"""


import news_reasoning as nr


class FakeDecider:
    """返回固定文本的假 LLM。"""

    def __init__(self, text):
        self.text = text

    def generate(self, prompt, timeout=90):
        return self.text


# ---------------- match_board ----------------


def test_match_board_exact():
    board_map = {"低空经济": "BK0001", "半导体": "BK0002"}
    assert nr.match_board("低空经济", board_map) == ("低空经济", "BK0001")


def test_match_board_fuzzy():
    board_map = {"粮食概念": "BK1086", "半导体": "BK0002"}
    # LLM 说"粮食"能匹配到"粮食概念"
    assert nr.match_board("粮食", board_map) == ("粮食概念", "BK1086")
    # 后缀省略: 说"半导体板块"匹配"半导体"
    assert nr.match_board("半导体板块", board_map) == ("半导体", "BK0002")


def test_match_board_none():
    assert nr.match_board("不存在的概念", {"低空经济": "BK1"}) == (None, None)
    assert nr.match_board("", {"低空经济": "BK1"}) == (None, None)


# ---------------- _extract_json ----------------


def test_extract_json_plain():
    assert nr._extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_fenced():
    assert nr._extract_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_extract_json_with_prefix():
    assert nr._extract_json('结果是:\n[{"idx": 3}]') == [{"idx": 3}]


def test_extract_json_garbage():
    assert nr._extract_json("没有 json") is None


# ---------------- extract_events ----------------


def test_extract_events_parses():
    news = [{"title": "t1", "summary": "s1", "time": "2026-09-01 10:00"}]
    decider = FakeDecider(
        '[{"idx": 1, "event": "XX政策发布", "direction": "利好", "concepts": ["半导体"], "significance": 8}]'
    )
    events = nr.extract_events(news, decider)
    assert len(events) == 1
    assert events[0]["event"] == "XX政策发布"
    assert events[0]["news"]["title"] == "t1"  # 原新闻回填


def test_extract_events_empty():
    news = [{"title": "t1", "summary": "s1"}]
    events = nr.extract_events(news, FakeDecider("[]"))
    assert events == []


def test_extract_events_bad_output():
    news = [{"title": "t1", "summary": "s1"}]
    assert nr.extract_events(news, FakeDecider("解析失败")) == []


# ---------------- 卡片与文本 ----------------


def test_build_card():
    events = [
        {
            "event": "政策发布",
            "direction": "利好",
            "boards": {"半导体": []},
            "reasoning": "因果链: A → B → C",
        }
    ]
    card = nr.build_card(events)
    assert "新闻掘金" in card["header"]["title"]["content"]
    assert "政策发布" in card["elements"][0]["text"]["content"]


def test_format_text():
    events = [
        {"event": "E1", "direction": "利好", "reasoning": "推理内容"},
    ]
    out = nr.format_text(events)
    assert "E1" in out and "推理内容" in out


# ---------------- split_reasoning(防重复) ----------------


def test_split_reasoning_per_event():
    """按事件标题切分,各事件拿到各自的段落。"""
    out = (
        "#### 事件1: 政策发布\n- **因果链**: A → B → C\n- **置信度**: 高\n\n"
        "#### 事件2: 订单中标\n- **因果链**: D → E → F\n- **置信度**: 中\n"
    )
    chunks = nr.split_reasoning(out, 2)
    assert len(chunks) == 2
    assert "政策发布" in chunks[0] and "A → B → C" in chunks[0]
    assert "订单中标" in chunks[1] and "D → E → F" in chunks[1]
    assert "订单中标" not in chunks[0]


def test_split_reasoning_no_headers():
    """LLM 没按格式输出时,全文给事件1,其余为空(不重复)。"""
    chunks = nr.split_reasoning("一段没有格式的推理", 3)
    assert chunks[0] == "一段没有格式的推理"
    assert chunks[1] == "" and chunks[2] == ""


def test_split_reasoning_missing_event():
    """部分事件缺标题时,缺失的给空串。"""
    out = "#### 事件1: 只有第一个\n因果链内容"
    chunks = nr.split_reasoning(out, 2)
    assert "只有第一个" in chunks[0]
    assert chunks[1] == ""


def test_split_reasoning_empty():
    assert nr.split_reasoning("", 2) == ["", ""]


def test_format_text_no_duplication():
    """多事件时推理不应整段重复 N 遍。"""
    events = [
        {"event": "E1", "direction": "利好", "reasoning": "推理1"},
        {"event": "E2", "direction": "利空", "reasoning": "推理2"},
    ]
    out = nr.format_text(events)
    assert out.count("推理1") == 1
    assert out.count("推理2") == 1


# ---------------- digest_news / build_card / _split_card_content ----------------


def test_digest_news_classifies():
    """digest_news 应把新闻传给 LLM 并返回分类清单。"""
    news = [
        {"title": "t1", "summary": "央行降准", "time": "2026-09-07 10:00"},
        {"title": "t2", "summary": "公司中标", "time": "2026-09-07 11:00"},
    ]
    out = nr.digest_news(news, FakeDecider("### 宏观\n- 10:00 央行降准\n"))
    assert "宏观" in out


def test_digest_news_skips_selected():
    """已深度分析过的新闻不应再出现在速览里。"""
    news = [
        {"title": "t1", "summary": "央行降准", "time": "2026-09-07 10:00"},
        {"title": "t2", "summary": "公司中标", "time": "2026-09-07 11:00"},
    ]
    out = nr.digest_news(news, FakeDecider("清单"), skip_titles={"央行降准"})
    assert "清单" in out  # 正常返回(跳过逻辑不炸)


def test_digest_news_empty():
    assert nr.digest_news([], FakeDecider("x")) == ""


def test_build_card_with_digest():
    events = [{"event": "E1", "direction": "利好", "boards": {}, "reasoning": "r"}]
    card = nr.build_card(events, digest="### 宏观\n- 10:00 某新闻")
    content = "".join(el["text"]["content"] for el in card["elements"])
    assert "其余要闻速览" in content
    assert "某新闻" in content


def test_split_card_content_no_truncate():
    """内容超 3000 字应切多个 div,不截断丢失。"""
    text = "行内容\n" * 800  # 3200 字
    parts = nr._split_card_content(text, size=3000)
    assert len(parts) > 1
    assert "".join(p.replace("\n", "") for p in parts).count("行内容") == 800
