# -*- coding: utf-8 -*-
"""网关配置：默认值写死在这里，<data>/config.json 里的同名键覆盖。"""
from __future__ import annotations
import json
import os

DEFAULTS = {
    "port": 8500,
    "bind": "0.0.0.0",
    "provider": "songkey",
    "model": "songkey-auto",
    "turn_timeout_sec": 60,
    "lock_timeout_sec": 70,
    "budget": {"base": 30, "factor": 2.5, "min": 40, "max_group": 150, "max_private": 220},
    "max_bubbles_group": 2,
    "max_bubbles_private": 3,
    "recent_global": 50,
    "recent_per_conversation": 20,
    "repeat_threshold": 0.5,
    "memory_max_bytes": 4096,
    "prime_count": 20,
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
