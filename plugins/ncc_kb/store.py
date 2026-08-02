# -*- coding: utf-8 -*-
"""ncc_kb 插件配置存取（带 mtime 缓存 + 原子写），与 ncc_community.store 同套路。"""
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
    # 知识库 OpenAI 兼容端点（mac-mini rag_proxy）。model 走 DusAPI 的 else 兜底(GPT 格式)。
    "endpoint": {
        "sdk": "DusAPI",
        "url": "http://100.71.182.5:8434",
        "key": "ncc-local",
        "model": "ncc-kb",
    },
    # 接入知识库时套用的人设 prompt 名（面板里已有的 prompt）。留空则不改人设。
    "prompt_name": "NCC肥肉",
    # 接入知识库的群聊名列表。写 "*" 表示所有群全开。
    "enabled_groups": ["肥肉测试1", "爱和一切肥肉测试群"],
    # 接入知识库的私聊对象名（who）列表。写 "*" 表示所有私聊全开。
    "enabled_chats": [],
    # 排除名单：即使上面写了 "*" 也不接知识库。排除优先于通配。
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
            save(copy.deepcopy(DEFAULT_CONFIG))
            return _cache
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 补齐缺失键，兼容旧文件
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
