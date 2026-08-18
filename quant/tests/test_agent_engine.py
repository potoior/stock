"""agent_engine 玉姐引擎单元测试：配置读写 + status 含 yujie 区块。"""

import json

import agent_engine
from agent_engine import (
    AgentEngine,
    _load_yujie_agent_config,
    _save_yujie_agent_config,
)


def test_yujie_agent_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_engine, "CONFIG_PATH", tmp_path / "no_cfg.json")
    cfg = _load_yujie_agent_config()
    assert cfg == {"min_score": 5, "max_hold_days": 20}


def test_yujie_agent_config_save_load(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(agent_engine, "CONFIG_PATH", cfg_path)
    # 写入既有 yujie 配置，验证保存不覆盖其他键
    cfg_path.write_text(json.dumps({"yujie": {"min_amount_yi": 1.5}, "other": 1}), encoding="utf-8")
    out = _save_yujie_agent_config({"min_score": 7, "max_hold_days": 30})
    assert out == {"min_score": 7, "max_hold_days": 30}
    # 重新读取
    assert _load_yujie_agent_config() == {"min_score": 7, "max_hold_days": 30}
    # 其他键保留
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["yujie"]["min_amount_yi"] == 1.5
    assert saved["other"] == 1


def test_yujie_agent_config_partial_update(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(agent_engine, "CONFIG_PATH", cfg_path)
    _save_yujie_agent_config({"min_score": 6})
    assert _load_yujie_agent_config() == {"min_score": 6, "max_hold_days": 20}
    _save_yujie_agent_config({"max_hold_days": 15})
    assert _load_yujie_agent_config() == {"min_score": 6, "max_hold_days": 15}


def test_agent_engine_status_has_yujie(monkeypatch, tmp_path):
    """AgentEngine.status() 必须含 yujie 区块与 yujie_config。用临时 db 避免污染。"""
    for attr in ("AI_DB", "RULE_DB", "YUJIE_DB", "LOG_DB"):
        monkeypatch.setattr(agent_engine, attr, tmp_path / f"{attr.lower()}.db")
    monkeypatch.setattr(agent_engine, "CONFIG_PATH", tmp_path / "no_cfg.json")
    agent_engine._init_log_db()  # 在 tmp LOG_DB 建表
    eng = AgentEngine()
    s = eng.status()
    assert "yujie" in s
    assert "yujie_config" in s
    assert "yujie_history" in s
    assert s["yujie_config"] == {"min_score": 5, "max_hold_days": 20}
    assert s["yujie"]["total_value"] == 10000.0
    assert s["yujie"]["positions"] == []
    # trades 支持 yujie 过滤
    assert eng.trades(type_filter="yujie") == []


def test_agent_engine_update_yujie_config(monkeypatch, tmp_path):
    for attr in ("AI_DB", "RULE_DB", "YUJIE_DB", "LOG_DB"):
        monkeypatch.setattr(agent_engine, attr, tmp_path / f"{attr.lower()}.db")
    monkeypatch.setattr(agent_engine, "CONFIG_PATH", tmp_path / "cfg.json")
    agent_engine._init_log_db()
    eng = AgentEngine()
    out = eng.update_yujie_config({"min_score": 7})
    assert out["min_score"] == 7
    assert eng.yujie_config["min_score"] == 7
