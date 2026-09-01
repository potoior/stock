"""news_monitor 单元测试:全部 mock/临时 db,不联网。"""

from datetime import datetime, timedelta

import news_monitor as nm


def test_classify():
    assert nm.classify("某公司发布减持公告") == "🔴"
    assert nm.classify("公司中标重大订单") == "🟢"
    assert nm.classify("今日股价平平") == "📝"


def test_within_hours():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    old_str = (datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")
    assert nm.within_hours(now_str) is True
    assert nm.within_hours(old_str) is False
    assert nm.within_hours("无法解析") is True  # 保守放行


def test_dedup_flow(tmp_path, monkeypatch):
    """同一条新闻 filter_new 只出现一次,mark_seen 后不再出现。"""
    db = tmp_path / "news_monitor.db"
    monkeypatch.setattr(nm, "DB_PATH", db)
    items = [
        {"title": "新闻A", "url": "http://x/a", "time": "2026-09-01 10:00", "summary": ""},
        {"title": "新闻B", "url": "http://x/b", "time": "2026-09-01 11:00", "summary": ""},
    ]
    # 第一次全new
    new = nm.filter_new("600519", items)
    assert len(new) == 2
    # 标记已见
    nm.mark_seen("600519", items)
    assert nm.filter_new("600519", items) == []
    assert nm.has_seen_any("600519") is True
    assert nm.has_seen_any("000001") is False
    # 新增一条只出来一条
    items.append({"title": "新闻C", "url": "http://x/c", "time": "2026-09-01 12:00", "summary": ""})
    new = nm.filter_new("600519", items)
    assert len(new) == 1 and new[0]["title"] == "新闻C"


def test_build_news_card():
    groups = [
        {"code": "600519", "name": "贵州茅台", "news": [{"title": "涨停", "time": "2026-09-01 10:00"}]},
    ]
    card = nm.build_news_card(groups)
    assert card["header"]["title"]["content"].startswith("📢 自选股新闻速递")
    body = card["elements"][0]["text"]["content"]
    assert "贵州茅台" in body and "涨停" in body


def test_run_once_empty_watchlist(monkeypatch, capsys):
    """自选池为空时跳过不报错。"""
    monkeypatch.setattr(nm, "load_group_watchlist", lambda: [])
    nm.run_once()
    assert "跳过" in capsys.readouterr().out


def test_run_once_first_time_baseline(monkeypatch, tmp_path):
    """首次监控只建基线不推送。"""
    monkeypatch.setattr(nm, "DB_PATH", tmp_path / "news_monitor.db")
    monkeypatch.setattr(
        nm, "load_group_watchlist",
        lambda: [{"code": "600519", "name": "贵州茅台"}],
    )
    monkeypatch.setattr(
        nm, "fetch_stock_news",
        lambda code, num=10: [{"title": "t", "url": "http://x/1", "time": "2026-09-01 10:00"}],
    )
    nm.run_once(dry_run=True)
    # 基线已建立
    assert nm.has_seen_any("600519") is True


def test_run_once_pushes_new(monkeypatch, tmp_path, capsys):
    """第二次运行有新消息时输出。"""
    monkeypatch.setattr(nm, "DB_PATH", tmp_path / "news_monitor.db")
    monkeypatch.setattr(
        nm, "load_group_watchlist",
        lambda: [{"code": "600519", "name": "贵州茅台"}],
    )
    items = [{"title": "新闻1", "url": "http://x/1", "time": "2026-09-01 10:00"}]
    monkeypatch.setattr(nm, "fetch_stock_news", lambda code, num=10: items)
    nm.run_once(dry_run=True)  # 首次:建基线
    items.append({"title": "新闻2", "url": "http://x/2", "time": "2026-09-01 11:00"})
    nm.run_once(dry_run=True)  # 第二次:应推送新闻2
    out = capsys.readouterr().out
    assert "新闻2" in out
