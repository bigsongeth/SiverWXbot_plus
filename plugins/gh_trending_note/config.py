# -*- coding: utf-8 -*-
"""gh_trending_note 插件配置。

与 ai_news_note 同构：可配项（开关/时间/目标）存 data/settings.json，
不常改的常量直接写在下面。register 时 importlib.reload(config) 拿最新值。
"""
import os
import json

_SETTINGS = os.path.join(os.path.dirname(__file__), "data", "settings.json")
_DEFAULTS = {
    "enabled": True,
    # AI 日报 09:10 发，这条排在它后面。真正的顺序保证不靠时间，
    # 靠 sender 里检查 ai_news_note 的 last_sent.txt（见 WAIT_FOR_AI_NEWS）。
    "send_time": "09:40",
    "target": "🏜️AI 及其代理人联邦",
}


def load_settings():
    try:
        with open(_SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULTS, **(data if isinstance(data, dict) else {})}
    except Exception:
        return dict(_DEFAULTS)


def save_settings(patch):
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


_s = load_settings()
ENABLED = _s["enabled"]
SEND_TIME = _s["send_time"]
TARGET = _s["target"]

# ---- 常量 ----
# mac-mini 的 launchd com.bigsong.gh-trending（每天 07:30）scp 覆盖推送到这里
DATA_FILE = r"C:\Users\Admin\gh_trending\latest.json"

TITLE_PREFIX = "📈 GitHub 趋势速览"
INTRO = "按新增 star 排的今日 / 本周 / 本月 Top5，逐个说清它是什么、值不值得看"

HIGHLIGHT = "#FFE066"        # 榜单条目标题底色（微信笔记只认 background-color）
# 相关度只剩这一个用途：给更值得看的项目换个底色。分数本身不再印在笔记里。
HIGHLIGHT_HOT = "#B7F0AD"
REL_MARK = 4

# 发送前，数据文件最多允许多旧（小时）
MAX_AGE_HOURS = 20

# 顺序保证：等 AI 日报先发完（读 ai_news_note/last_sent.txt 是否为今天）。
# 关掉它则两条日报互不等待。
WAIT_FOR_AI_NEWS = True

# 跟随模式：不死等 SEND_TIME，AI 日报一发完就接着发，两条消息在群里连着出现。
# SEND_TIME 退化成兜底（跟随没触发时到点照发，有 last_sent 防重不会重复）。
FOLLOW_AI_NEWS = True
# AI 日报发完后缓一会儿再动手：它刚占完微信 UI（关编辑器、切窗口），
# 立刻接着跑第二轮 UIA 容易撞在一起。
FOLLOW_DELAY_SEC = 90
# 跟随只在这个时间窗内允许触发，防止深夜往群里发消息。
# 场景：bot 半夜重启会把 _followed_on 这个内存闸门清掉，若当天恰好还没发成功、
# 数据又刚到位，下一个 tick 就会立刻开发 —— 凌晨三点在群里刷屏。
# 手动 flag 触发和 SEND_TIME 定时都不受本窗口限制（那是人明确要的）。
FOLLOW_WINDOW = ("08:00", "13:00")

# 详细展开哪些榜（其余榜走精简版式，控制笔记长度）
VERBOSE_SECTIONS = ("daily",)
