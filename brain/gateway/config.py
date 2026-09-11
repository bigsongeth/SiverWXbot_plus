# -*- coding: utf-8 -*-
"""网关配置：默认值写死在这里，<data>/config.json 里的同名键覆盖。"""
from __future__ import annotations
import json
import os

DEFAULTS = {
    "port": 8500,
    "bind": "127.0.0.1",   # /tool/* 与 /proposals 审批都没有鉴权，期 1 只准本机访问；期 2 容器化后再在 config.json 放开
    "provider": "songkey",
    "model": "songkey-auto",
    "turn_timeout_sec": 120,   # songkey-auto 目前落到 grok-4.6，一句话也要先烧 700 个推理 token（24s 起步）
    "lock_timeout_sec": 250,   # 主 prompt + nudge 各 120s 的最坏情况
    # 同时最多处理几条消息（不同会话之间；同一会话永远串行保序）。设 1 = 退回 2026-09-08 之前的
    # 全局串行行为。往上调之前想清楚 songkey 配额和这台机器的内存，美食群那种要串一堆 MCP 工具
    # 的轮次尤其吃资源。设计见 docs/superpowers/specs/2026-09-08-concurrency-design.md
    "max_concurrent": 3,
    # 连续多少轮超时才重建 dsh 进程。并发下不能一超时就杀进程（会误杀别人在飞的轮次），
    # 迟到回调靠 turn_id 挡住即可；进程真死了则不受这个计数约束，立刻重建。
    "restart_after_timeouts": 3,
    "budget": {"base": 30, "factor": 2.5, "min": 40, "max_group": 150, "max_private": 220},
    "max_bubbles_group": 2,
    "max_bubbles_private": 3,
    "recent_global": 50,
    "recent_per_conversation": 20,
    "repeat_threshold": 0.5,
    "memory_max_bytes": 4096,
    "prime_count": 20,
    "history_delta_max": 40,            # 预热后每轮最多带多少条"上次之后的新消息"（机器人每次传最近 60 条）
    "seen_max_per_conversation": 600,   # 每个会话记多少条"已看过"的 fingerprint
    "kb_url": "http://100.71.182.5:8434",
    "kb_timeout_sec": 20,
}


def load(data_dir: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(data_dir, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
