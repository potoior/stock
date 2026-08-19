"""tool_stats.py 单测: 审计日志统计 CLI。"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tool_stats


def _make_log(tmp_path: Path, records: list[dict]) -> Path:
    """生成临时 JSONL 日志。"""
    fp = tmp_path / "audit.jsonl"
    with open(fp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return fp


def _sample_records(n: int = 10) -> list[dict]:
    """生成 n 条样本记录。"""
    out = []
    now = datetime.now()
    for i in range(n):
        out.append({
            "ts": (now - timedelta(minutes=i)).isoformat(timespec="seconds"),
            "pid": 12345,
            "session_id": f"chatA:user{i % 3}",
            "step": (i % 3) + 1,
            "tool": ["analyze_stock", "get_market_status", "compare_stocks"][i % 3],
            "args": {"code": "600519"},
            "result_size": 200 + i * 50,
            "duration_ms": 100 + i * 20,
            "error": "timeout" if i % 5 == 0 else None,
        })
    return out


def test_load_records(tmp_path):
    """正常 JSONL 应能全部加载。"""
    fp = _make_log(tmp_path, _sample_records(5))
    records = tool_stats.load_records(fp)
    assert len(records) == 5
    assert records[0]["tool"] == "analyze_stock"


def test_load_records_skips_bad_lines(tmp_path):
    """坏行应被跳过,不抛异常。"""
    fp = tmp_path / "audit.jsonl"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-08-19", "tool": "x"}) + "\n")
        f.write("not a json\n")
        f.write(json.dumps({"ts": "2026-08-19", "tool": "y"}) + "\n")
        f.write("\n")  # 空行
    records = tool_stats.load_records(fp)
    assert len(records) == 2


def test_load_records_missing_file():
    """文件不存在应返回空列表。"""
    records = tool_stats.load_records(Path("/nonexistent/audit.jsonl"))
    assert records == []


def test_parse_ts_valid():
    """合法 ISO 时间应能解析。"""
    rec = {"ts": "2026-08-19T17:02:54"}
    ts = tool_stats.parse_ts(rec)
    assert ts is not None
    assert ts.year == 2026


def test_parse_ts_invalid():
    """非法时间应返回 None。"""
    assert tool_stats.parse_ts({"ts": "not-a-date"}) is None
    assert tool_stats.parse_ts({}) is None


def test_filter_records_hours(tmp_path):
    """按小时过滤: 只保留最近 N 小时内的记录。"""
    now = datetime.now()
    records = [
        {"ts": (now - timedelta(minutes=30)).isoformat(timespec="seconds"), "tool": "x"},
        {"ts": (now - timedelta(hours=2)).isoformat(timespec="seconds"), "tool": "y"},
    ]
    out = tool_stats.filter_records(records, hours=1, since=None,
                                     session=None, tool=None, errors_only=False)
    assert len(out) == 1
    assert out[0]["tool"] == "x"


def test_filter_records_session(tmp_path):
    """按会话过滤。"""
    records = [
        {"ts": datetime.now().isoformat(timespec="seconds"), "session_id": "A", "tool": "x"},
        {"ts": datetime.now().isoformat(timespec="seconds"), "session_id": "B", "tool": "y"},
    ]
    out = tool_stats.filter_records(records, hours=None, since=None,
                                     session="A", tool=None, errors_only=False)
    assert len(out) == 1
    assert out[0]["session_id"] == "A"


def test_filter_records_tool(tmp_path):
    """按工具过滤。"""
    records = [
        {"ts": datetime.now().isoformat(timespec="seconds"), "tool": "analyze_stock"},
        {"ts": datetime.now().isoformat(timespec="seconds"), "tool": "compare_stocks"},
    ]
    out = tool_stats.filter_records(records, hours=None, since=None,
                                     session=None, tool="analyze_stock", errors_only=False)
    assert len(out) == 1
    assert out[0]["tool"] == "analyze_stock"


def test_filter_records_errors_only(tmp_path):
    """只看错误。"""
    records = [
        {"ts": datetime.now().isoformat(timespec="seconds"), "tool": "x", "error": None},
        {"ts": datetime.now().isoformat(timespec="seconds"), "tool": "y", "error": "timeout"},
    ]
    out = tool_stats.filter_records(records, hours=None, since=None,
                                     session=None, tool=None, errors_only=True)
    assert len(out) == 1
    assert out[0]["error"] == "timeout"


def test_filter_records_since(tmp_path):
    """按起始日期过滤。"""
    records = [
        {"ts": "2026-08-19T10:00:00", "tool": "x"},
        {"ts": "2026-08-18T10:00:00", "tool": "y"},
    ]
    out = tool_stats.filter_records(records, hours=None, since="2026-08-19",
                                     session=None, tool=None, errors_only=False)
    assert len(out) == 1
    assert out[0]["tool"] == "x"


def test_main_no_log_file(capsys):
    """日志文件不存在时应退出码 1。"""
    with pytest.raises(SystemExit) as exc:
        tool_stats.main.__wrapped__ if hasattr(tool_stats.main, "__wrapped__") else None
        sys.argv = ["tool_stats.py", "--log", "/nonexistent/audit.jsonl"]
        tool_stats.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "不存在" in captured.err


def test_main_empty_log(tmp_path, capsys):
    """空日志应退出码 0。"""
    fp = tmp_path / "empty.jsonl"
    fp.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        sys.argv = ["tool_stats.py", "--log", str(fp)]
        tool_stats.main()
    assert exc.value.code == 0


def test_main_normal_output(tmp_path, capsys):
    """正常日志应输出总览+按工具+按会话。"""
    fp = _make_log(tmp_path, _sample_records(10))
    sys.argv = ["tool_stats.py", "--log", str(fp), "--hours", "0"]
    tool_stats.main()
    out = capsys.readouterr().out
    assert "总览" in out
    assert "按工具" in out
    assert "按会话" in out
    assert "analyze_stock" in out


def test_main_errors_only(tmp_path, capsys):
    """--errors 应输出错误明细。"""
    fp = _make_log(tmp_path, _sample_records(10))
    sys.argv = ["tool_stats.py", "--log", str(fp), "--hours", "0", "--errors",
                "--no-by-tool", "--no-by-session"]
    tool_stats.main()
    out = capsys.readouterr().out
    assert "错误明细" in out
    assert "timeout" in out


def test_main_tail(tmp_path, capsys):
    """--tail N 应输出最近 N 条明细。"""
    fp = _make_log(tmp_path, _sample_records(10))
    sys.argv = ["tool_stats.py", "--log", str(fp), "--hours", "0", "--tail", "5",
                "--no-overview", "--no-by-tool", "--no-by-session"]
    tool_stats.main()
    out = capsys.readouterr().out
    assert "最近 5 条明细" in out
