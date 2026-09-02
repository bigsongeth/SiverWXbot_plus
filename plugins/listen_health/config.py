# -*- coding: utf-8 -*-
"""插件配置读取。改 data/config.json 下一次调用即生效，不用重启（探针间隔除外，见 probe.py）。"""
from __future__ import annotations

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

DEFAULTS = {
    "alert": {
        "enabled": True,
        "cooldown_sec": 600,      # 同一会话多久内只告警一次，防刷屏
        "webhook": True,
        "admin_group": True,
    },
    "probe": {
        "enabled": True,
        "interval_min": 10,
        "target": "文件传输助手",  # 拿系统会话当靶子，不打扰真人、不产生已读
        # 连续失败几次才发普通告警。开着自愈时通常轮不到它（2 次就重启并另发通知了），
        # 它主要服务于 auto_restart=false 的场景。
        "alert_after_consecutive": 3,
        # --- 自愈（见 heal.py）---
        "auto_restart": True,
        "restart_after_consecutive": 2,   # 连续 2 次 ≈ 20 分钟，避开单次抖动
        "restart_cooldown_min": 60,       # 冷却期内不再重启；期内又失败 = 重启无效，叫人
        "restart_task_name": "SWXPanelRestart",
    },
    # --- 开窗录像机（见 tap.py）：AddListenChat 期间录底层点击消息，失败时连截图一起存档 ---
    "tap": {
        "enabled": True,
        "screenshot": True,       # 失败时截屏（PIL.ImageGrab，约 200ms，只在失败时做）
        "keep_success": True,     # 成功也记一行（只有调用序列，没有截图），用来和失败对比
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """读配置，文件缺失/损坏一律回落默认值（这插件不该因为配置问题拖垮 bot）。"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return _merge(DEFAULTS, json.load(f))
    except Exception:
        return dict(DEFAULTS)
