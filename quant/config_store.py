"""config.json 统一配置层(唯一读写入口)。

之前 7 个模块各自 json.loads/write_text 直接读写 config.json:
  - 缓存策略各不相同(有的有 mtime 缓存,有的每次现读)
  - 写一半崩溃会留下残缺文件
  - 测试要分别在 5 个模块上 monkeypatch CONFIG_PATH

现在全部经由本模块:
  - load_config()/save_config(): 全量读写
  - get_section()/set_section()/update_section(): 顶层 section 读写
  - mtime 进程级缓存,外部改文件自动失效
  - config.json 缺失时自动用 config.example.json 初始化(保持原行为)
  - 原子写(tmp + os.replace),写一半崩溃不损坏原文件

注意: load_config() 返回缓存对象,调用方原地修改后应调用 save_config()
落盘,不要改了缓存不保存。测试统一 monkeypatch 本模块的 CONFIG_PATH。
"""

import json
import os
import threading
from pathlib import Path

ENGINE_HOME = Path(__file__).parent
CONFIG_PATH = ENGINE_HOME / "config.json"

_lock = threading.RLock()
_cache: dict = {"key": None, "data": None}


def _read_all(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def load_config() -> dict:
    """读全量配置。

    config.json 缺失或损坏时,用 config.example.json 初始化后返回;
    都失败返回空 dict。
    """
    with _lock:
        path = CONFIG_PATH
        key = None
        try:
            st = path.stat()
            # mtime + size 双保险:部分文件系统 mtime 粒度粗,连续写可能同时间戳
            key = (str(path), st.st_mtime_ns, st.st_size)
            if _cache["key"] == key and _cache["data"] is not None:
                return _cache["data"]
        except OSError:
            pass
        if path.exists():
            data = _read_all(path)
            if data is not None:
                _cache["key"] = key
                _cache["data"] = data
                return data
        # 缺失或损坏:用 example 初始化(保持原 strategy_engine 行为)
        example = ENGINE_HOME / "config.example.json"
        if example.exists():
            data = _read_all(example)
            if data is not None:
                try:
                    save_config(data)
                    return data
                except Exception:
                    pass
        return {}


def save_config(cfg: dict) -> None:
    """全量写回(原子写:先写 tmp 再 os.replace)。"""
    with _lock:
        path = CONFIG_PATH
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        try:
            st = path.stat()
            _cache["key"] = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        _cache["data"] = cfg


def get_section(key: str) -> dict:
    """读顶层 section,缺失或非 dict 返回空字典。"""
    val = load_config().get(key)
    return val if isinstance(val, dict) else {}


def set_section(key: str, value) -> None:
    """写顶层 section。"""
    with _lock:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)


def update_section(key: str, updates: dict) -> dict:
    """合并更新顶层 section,返回更新后的 section。"""
    with _lock:
        cur = get_section(key)
        cur.update(updates or {})
        set_section(key, cur)
        return cur
