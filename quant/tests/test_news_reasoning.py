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
