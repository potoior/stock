"""config_store 统一配置层测试。"""

import json

import pytest

import config_store


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", path)
    # 每个测试独立缓存隔离
    monkeypatch.setattr(config_store, "_cache", {"key": None, "data": None})
    return path


def test_load_missing_returns_example(cfg_path):
    """config.json 缺失时,用 config.example.json 自动初始化。"""
    cfg = config_store.load_config()
    assert cfg.get("watchlist") == []
    assert cfg_path.exists()
    assert isinstance(cfg.get("strategies"), list) and cfg["strategies"]


def test_save_and_load_roundtrip(cfg_path):
    config_store.save_config({"feishu": {"chat_id": "oc_123"}})
    cfg = config_store.load_config()
    assert cfg["feishu"]["chat_id"] == "oc_123"


def test_mtime_cache(cfg_path):
    """外部改文件,缓存自动失效(mtime/size 变化)。"""
    cfg_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert config_store.load_config() == {"a": 1}

    # 注: 同尺寸同 mtime 的微秒级连续写无法感知(与原 mtime 缓存同样的边界)
    cfg_path.write_text(json.dumps({"a": 22}), encoding="utf-8")
    assert config_store.load_config() == {"a": 22}

    cfg_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert config_store.load_config() == {"a": 1}


def test_corrupt_file_falls_back_to_example(cfg_path):
    """损坏的 config.json 被用 example 覆盖(保持原 strategy_engine 行为)。"""
    cfg_path.write_text("{not valid json", encoding="utf-8")
    cfg = config_store.load_config()
    assert isinstance(cfg.get("strategies"), list)


def test_get_section_missing(cfg_path):
    assert config_store.get_section("no_such") == {}


def test_get_section_non_dict(cfg_path):
    config_store.save_config({"feishu": "oops"})
    assert config_store.get_section("feishu") == {}


def test_set_section(cfg_path):
    config_store.save_config({"feishu": {"chat_id": "oc_old"}})
    config_store.set_section("yujie", {"min_score": 5})
    cfg = config_store.load_config()
    assert cfg["feishu"]["chat_id"] == "oc_old"
    assert cfg["yujie"] == {"min_score": 5}


def test_update_section(cfg_path):
    config_store.update_section("yujie_agent", {"min_score": 6})
    config_store.update_section("yujie_agent", {"max_hold_days": 30})
    cfg = config_store.load_config()
    assert cfg["yujie_agent"] == {"min_score": 6, "max_hold_days": 30}


def test_no_tmp_left_behind(cfg_path):
    config_store.save_config({"a": 1})
    tmp = cfg_path.parent / (cfg_path.name + ".tmp")
    assert not tmp.exists()
