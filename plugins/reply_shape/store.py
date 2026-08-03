# -*- coding: utf-8 -*-
"""reply_shape 配置存取（mtime 缓存 + 原子写），与 ncc_kb.store 同套路。"""
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
    "enabled": True,
    # 短于这个字数的分条会被并进相邻条。20 字够抓住"需要我继续挖？发个信号就行。"
    # 这类收尾邀请，又不会误伤真正短但有信息的句子（那种通常整条回复就一句、不分条）。
    "min_chars": 20,
    # 留空则用代码里的 EXTRA_RULE 默认文案；想调措辞在这里覆盖，改完下一条消息即生效。
    "extra_rule": "",
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
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
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


def enabled() -> bool:
    return bool(load().get("enabled", True))
