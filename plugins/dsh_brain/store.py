# -*- coding: utf-8 -*-
"""dsh_brain 插件配置（带 mtime 缓存 + 原子写，与 ncc_kb.store 同套路）。改配置下一条消息生效，不用重启。"""
from __future__ import annotations

import copy
import json
import os
import threading

_LOCK = threading.RLock()
_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULT_CONFIG = {
    # 总开关。默认关：合进 main 不改任何行为，要测再打开。
    "enabled": False,
    # 大脑网关（brain/run.py）地址。期 1 跑在 mac 上，走 Tailscale。
    "gateway_url": "http://100.127.39.63:8500",
    # 等大脑一轮的上限，必须 ≥ 网关 lock_timeout_sec（250）：网关串行，排队 + 一轮 8–115 秒。
    "timeout_sec": 300,
    # 走大脑的群 / 私聊对象名；写 "*" 表示该类全开。排除名单优先于通配。
    "enabled_groups": [],
    "enabled_chats": [],
    "excluded_groups": [],
    "excluded_chats": [],
}

_cache = None
_cache_mtime = None


def load() -> dict:
    global _cache, _cache_mtime
    with _LOCK:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if _cache is not None and mtime is not None and mtime == _cache_mtime:
            return _cache
        if mtime is None:
            # 文件缺失不落盘：默认全关，没必要在生产目录里凭空生成一个文件
            _cache = copy.deepcopy(DEFAULT_CONFIG)
            _cache_mtime = None
            return _cache
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        _cache = cfg
        _cache_mtime = mtime
        return _cache


def save(cfg: dict) -> None:
    global _cache, _cache_mtime
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _cache = cfg
        try:
            _cache_mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            _cache_mtime = None
