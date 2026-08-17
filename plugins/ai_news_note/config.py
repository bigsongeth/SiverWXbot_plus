# -*- coding: utf-8 -*-
"""ai_news_note 插件配置。

用户可在面板 /ai_news 页改的项（开关/时间/目标）存在 data/settings.json，面板读写；
不常改的常量（数据源/文案/样式）直接写在下面。
register_daily_note 里 importlib.reload(config) 会重跑本模块，从而拿到最新 settings（热更新）。
"""
import os
import json

_SETTINGS = os.path.join(os.path.dirname(__file__), "data", "settings.json")
_DEFAULTS = {
    "enabled": True,
    "send_time": "09:10",              # 24h，本机时区（日报 09:00 推来，留富余）
    "target": "🏜️AI 及其代理人联邦",   # 微信群名/联系人名，需能被搜索唯一命中
}


def load_settings():
    """读 settings.json，缺字段用默认补齐。文件不存在返回默认。"""
    try:
        with open(_SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULTS, **(data if isinstance(data, dict) else {})}
    except Exception:
        return dict(_DEFAULTS)


def save_settings(patch):
    """把 patch 里的 enabled/send_time/target 合并写回 settings.json，返回完整配置。"""
    cur = load_settings()
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    if str(patch.get("send_time", "")).strip():
        cur["send_time"] = str(patch["send_time"]).strip()
    if str(patch.get("target", "")).strip():
        cur["target"] = str(patch["target"]).strip()
    os.makedirs(os.path.dirname(_SETTINGS), exist_ok=True)
    with open(_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return cur


# ---- 从 settings.json 读出的可配项（模块级；reload 时刷新）----
_s = load_settings()
ENABLED = _s["enabled"]
SEND_TIME = _s["send_time"]
TARGET = _s["target"]

# ---- 常量（不常改，直接改这里）----
# 日报数据文件（mac-mini 每天覆盖推送）
DATA_FILE = r"C:\Users\Admin\ai_news\latest.json"

# 笔记标题里的品牌前缀 + 第一段介绍语（这两行=转发缩略卡片显示内容）
TITLE_PREFIX = "🐶 肥肉 AI 精选"
INTRO = "不追刚刚发生的热闹，只捡经得起放凉的思考"

# 每条标题的高亮背景色（微信笔记只认 background-color，不认文字 color / <mark>）
HIGHLIGHT = "#FFE066"

# 发送前，日报文件最多允许多旧（小时）。超过则跳过发送，避免发到隔夜/陈旧数据。
MAX_AGE_HOURS = 20


# ---- 降级通路（笔记开不出来时的止血；判据见 sender.py 的 _DEGRADE_SAFE_STAGES）----
# 2026-08-12：微信进入「已有窗口读写正常、新顶层窗口一个都建不出来」的状态时，建笔记
# 必然失败（当天连败 4 次、日报断供一整天），而往已有窗口发纯文本全程正常。
DEGRADE_ENABLED = False   # 2026-08-12 23:xx 关闭：这条通路未经用户授权就上线并真发了一条群消息
# 'digest' 只发 序号+标题 / 评分+来源 / 链接（实测 12 条约 1.4k 字，一条消息就发完）；
# 'full'   额外带摘要与关键词（实测 12 条约 4.2k 字，要分 4 条发）。
DEGRADE_MODE = "digest"
DEGRADE_SEG_CHARS = 1500          # 单条消息字数上限（条目块不切开，实际略小于此值）
DEGRADE_MAX_SEGMENTS = 6          # 最多发几条，防刷屏/风控；超出部分丢弃并在末尾注明
DEGRADE_SEG_INTERVAL_SEC = 4.0    # 条与条之间的间隔（节流）
DEGRADE_AFTER_FAILS = 1           # 笔记流程「安全失败」累计几次后降级
DEGRADE_AFTER_MIN = 40            # 或距今天首次失败满多少分钟就降级（防重启丢掉重试链）
DEGRADE_NOTE = "（笔记通道故障，本次改为文本推送）"
DEGRADE_DRY_RUN = False           # True = 只把要发的内容写进日志，一条都不真发（验证用）
