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
    # 回复长度预算。两维（办事/闲聊 × 想不想深入）都由代码从对方消息里算，模型不参与。
    # mode 改回 "legacy" 立刻退回旧公式 base + factor×入长（base/factor 仅 legacy 用）。
    # 设计与实测数据见 docs/superpowers/specs/2026-09-11-reply-length-design.md
    "budget": {
        "mode": "two_axis",
        "base_chat": 45,          # 闲聊基数
        "base_errand": 85,        # 办事基数（问据点/价格/报名/技术问题）
        "deep_factor": 1.7,       # 对方明说要展开（「详细讲讲」）——强信号
        "followup_factor": 1.25,  # 对方在追问同一话题——弱信号（连续问币价也算追问，但那不是想听长回答）
        "long_input_chars": 60,   # 办事类提问超过这个长度，视同明说要展开（认真打一长段=投入度信号）
        "len_factor": 0.5,        # 入长只作弱信号（旧公式是 2.5，它让灌水长消息顶格 220）
        "min": 55, "max_group": 150, "max_private": 220,
        "overflow_tolerance": 1.3,  # 超预算多少以内直接按句子裁、不退回重写（生产 53% 的轮次在跑第二遍）
        "base": 30, "factor": 2.5,  # legacy 模式用
    },
    "fullwidth_punct": True,      # 把中文语境里的半角标点转成全角
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
