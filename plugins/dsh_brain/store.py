# -*- coding: utf-8 -*-
"""dsh_brain 插件配置（带 mtime 缓存 + 原子写，与 ncc_community.store 同套路）。改配置下一条消息生效，不用重启。"""
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
    # 大脑一轮失败（超时/出错/网关不可达）后再起几轮。2026-09-07 用户拍板：别切备用接口，失败就起新的一轮。
    # 每轮最长 timeout_sec，主循环是串行的，这段时间所有会话都在等，所以别设太大。
    "max_attempts": 2,
    "retry_delay_sec": 3,
    # 重试也耗尽时回这句（原样发出去）。留空 = 退回老行为：交给 model_fallback 切备用接口。
    "exhausted_reply": "🐶 脑子刚才卡住了，这条没处理成，过会儿再 @ 我一次",
    # ---- 并发回复（2026-09-08，见 dispatch.py 与 docs/superpowers/specs/2026-09-08-concurrency-design.md）----
    # 总开关，默认关：合进 main 不改任何行为。开了之后，命中大脑的会话不再占着监听线程等回复，
    # 整条消息的处理挪到 worker 线程，别的群立刻能被读到。关掉即刻退回同步行为。
    "async_reply": False,
    # 同时最多处理几个会话。**应与网关 max_concurrent 一致**：这边开得比网关大，多出来的请求
    # 只会堆在网关的信号量上排队（拿不到就是 busy），白占 worker。
    "max_workers": 3,
    # 单会话待处理上限，超了丢最旧的（群聊刷屏时回最新的那条更有意义，见 dispatch.submit）。
    "queue_max_per_conv": 5,
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
        for k, v in DEFAULT_CONFIG.items():   # 与 load() 同样补默认值，别让 save 之后的缓存缺键
            cfg.setdefault(k, copy.deepcopy(v))
        _cache = cfg
        try:
            _cache_mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            _cache_mtime = None
