# -*- coding: utf-8 -*-
"""ai_news_note 插件：每日 AI 日报 -> 微信收藏笔记 -> 发送到目标群。

集成方式（wxbot_core.py 里最小 hook，约 6 行）：
    from plugins.ai_news_note import register_daily_note
    register_daily_note(self, schedule)

业务逻辑全在本插件，不侵入核心。配置见 config.py。
"""
import importlib
from . import config
from . import trigger
from .sender import send_daily_note, log


def register_daily_note(bot, schedule):
    """把每日发送任务注册进 bot 的 schedule。由 wxbot_core 在定时任务注册处调用。"""
    # 热更新：重新读 config.py，使 /stop_bot + /start_bot 即可让改动生效，无需重启 web_server。
    # （sender/render 都用 `config.X` 动态取值，reload 后自动拿新值）
    importlib.reload(config)
    schedule.clear("ai_news_note")  # 幂等：重复注册先清旧
    if not config.ENABLED:
        bot._ai_news_note_enabled = False
        log("插件已禁用，未注册定时任务")
        return
    # 标志位：让主循环的 schedule.run_pending() 在本插件启用时也执行
    # （核心默认只在 定时消息/定时朋友圈 开关打开时才 run_pending）
    bot._ai_news_note_enabled = True
    schedule.every().day.at(config.SEND_TIME).do(_job, bot).tag("ai_news_note")
    # 外部触发 + 失败重试，都在主循环里跑（见 trigger.py 顶部：独立进程会和 bot 抢微信 UI）
    schedule.every(10).seconds.do(_tick, bot).tag("ai_news_note")
    log(f"已注册每日 AI 日报笔记任务：{config.SEND_TIME} -> {config.TARGET}"
        f"（外部触发与失败重试已挂载）")


def _job(bot):
    """schedule 回调：每日定时跑一次发送（失败会自动安排重试）。"""
    return trigger.start_day(bot, source="scheduled")


def _tick(bot):
    """schedule 回调：每 10 秒看一眼有没有 mac-mini 的发送请求 / 到点的重试。"""
    return trigger.tick(bot)
