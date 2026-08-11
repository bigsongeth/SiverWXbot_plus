# -*- coding: utf-8 -*-
"""gh_trending_note 插件：每日 GitHub 趋势 -> 微信收藏笔记 -> 发送到目标群。

和 ai_news_note 是平行的两条管线，共用后者的 UIA 原语与敏感词表，但配置、
数据源、防重文件、日志各自独立 —— 这样改这条不会碰坏已经在跑的 AI 日报。

集成方式（wxbot_core.py 里最小 hook，2 行，紧跟 ai_news_note 那两行）：
    from plugins.gh_trending_note import register_trending_note
    register_trending_note(self, schedule)
"""
import importlib
from . import config
from . import trigger
from .sender import send_trending_note, log


def register_trending_note(bot, schedule):
    """把每日发送任务注册进 bot 的 schedule。由 wxbot_core 在定时任务注册处调用。"""
    importlib.reload(config)
    schedule.clear("gh_trending_note")
    if not config.ENABLED:
        log("插件已禁用，未注册定时任务")
        return
    # 复用 ai_news_note 立的那面旗子：核心默认只在 定时消息/定时朋友圈 开关打开时
    # 才跑 schedule.run_pending()，这个标志让它无条件跑。
    bot._ai_news_note_enabled = True
    schedule.every().day.at(config.SEND_TIME).do(_job, bot).tag("gh_trending_note")
    schedule.every(10).seconds.do(_tick, bot).tag("gh_trending_note")
    log("已注册每日 GitHub 趋势笔记任务：{} -> {}（外部触发与失败重试已挂载）".format(
        config.SEND_TIME, config.TARGET))


def _job(bot):
    return trigger.start_day(bot, source="scheduled")


def _tick(bot):
    return trigger.tick(bot)
