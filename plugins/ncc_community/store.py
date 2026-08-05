# -*- coding: utf-8 -*-
"""ncc_community 插件配置存取。

配置文件 data/config.json，缺失时用 DEFAULT_CONFIG 自动生成。
读带 mtime 缓存（手工改文件后自动重载），写走临时文件替换防半截 JSON。
"""
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
    # 管理群：群内所有成员都是管理员
    "admin_group": "NCC 社群管理肥肉售后维权🤖",
    # 管理面板地址（「后台」指令回给管理群的链接）。留空则用 panel.DEFAULT_PANEL_URL。
    # 走 Tailscale IP，局域网换段也不用改（CLAUDE.md 3.1）。
    "panel_url": "http://100.73.185.46:10001/ncc_community",
    "forward": {
        # 收集模式空闲多少秒后自动结束
        "session_timeout": 300,
        # 单次转发操作最多勾选的目标数（超过则分批）
        "chunk_size": 8,
        # 分组名 -> 目标群列表
        "groups": {
            "测试组": ["肥肉测试1", "爱和一切肥肉测试群"]
        },
    },
    "welcome": {
        # 群名 -> 迎新配置；text 中 {name} 会替换成新人昵称
        "肥肉测试1": {
            "enabled": True,
            "text": "欢迎 {name} 加入！🎉 我是肥肉，有问题群里@我~",
            "url": ""
        }
    },
    "invite": {
        # 关键词 -> 目标群
        "keywords": {"测试拉群": "肥肉测试1"},
        # 每人每个关键词每天最多触发次数
        "daily_limit": 3,
    },
}

_cache = None
_cache_mtime = None


def load() -> dict:
    """读取插件配置。文件不存在时落盘默认配置。"""
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
            _cache = json.load(f)
        _cache_mtime = mtime
        return _cache


def save(cfg: dict) -> None:
    """原子化写入配置并刷新缓存。"""
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
