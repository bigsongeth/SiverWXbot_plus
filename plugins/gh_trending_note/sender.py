# -*- coding: utf-8 -*-
"""gh_trending_note 发送编排。

设计原则：**一行 UIA 代码都不自己写**。建笔记/找笔记/打开群/发送收藏这些原语
全部 import 自 plugins.ai_news_note.sender —— 那套逻辑是 2026-07~08 用一堆线上事故
换来的（锁屏检测、topmost 抢前台、粘贴回读校验、按宽度区分发送按钮…），
重写一遍只会把同样的坑再踩一次。本模块只负责：读自己的数据、渲染、按自己的规则编排。

同样的硬前提：本模块在 bot 进程内、由 schedule 主循环单线程调用，
和 bot 其它 UI 操作串行。**绝不能另起进程跑**（会和 bot 抢微信 UI，见 trigger.py 顶部）。
"""
import os
import json
import time
import datetime

from . import config
from .render import render, count_items

# 复用 ai_news_note 久经考验的 UIA 原语（它们都不依赖 ai_news_note.config）
from plugins.ai_news_note.sender import (
    _build_cf_html, _clip_html, _desktop_usable, _close_update_windows,
    _close_all_editors, _create_note_from_clipboard, _find_note_cell,
    _open_target, _send_favorite, _drop_topmost,
)

_LOG = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "gh_trending_note.log")
_STATE = os.path.join(os.path.dirname(__file__), "last_sent.txt")
# AI 日报的防重文件——用它判断"AI 日报今天发了没"，实现两条日报的先后顺序
_AI_NEWS_STATE = os.path.join(os.path.dirname(__file__), "..", "ai_news_note", "last_sent.txt")


def log(msg):
    line = "[{}] {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print("[GitHub趋势] " + str(msg))
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _fresh_enough(d):
    ga = d.get("generated_at")
    if not ga:
        return True
    try:
        t = datetime.datetime.strptime(ga, "%Y-%m-%d %H:%M:%S")
        return (datetime.datetime.now() - t).total_seconds() / 3600.0 <= config.MAX_AGE_HOURS
    except Exception:
        return True


def _already_sent_today():
    try:
        with open(_STATE, encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except Exception:
        return False


def _mark_sent_today():
    try:
        with open(_STATE, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
    except Exception:
        pass


def _ai_news_sent_today():
    """AI 日报今天发过了没。读不到就当没发（宁可等，不抢在它前面）。"""
    try:
        with open(_AI_NEWS_STATE, encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except Exception:
        return False


def ai_news_sent_at():
    """AI 日报今天发成功的时间戳；今天没发过返回 0。

    用 last_sent.txt 的 mtime —— 那个文件是 ai_news_note 发送成功后才写的，
    写入时间就等于发送完成时间。跟随模式靠它判断"日报刚发完多久了"。
    """
    try:
        with open(_AI_NEWS_STATE, encoding="utf-8") as f:
            if f.read().strip() != datetime.date.today().isoformat():
                return 0
        return os.path.getmtime(_AI_NEWS_STATE)
    except Exception:
        return 0


def already_sent_today():
    """给 trigger 用的公开别名。"""
    return _already_sent_today()


def _push(msg, source):
    try:
        from webhook_send import send_message
        tag = "手动" if source == "manual" else "定时"
        send_message("📈 GitHub 趋势 · {}".format(tag), msg)
    except Exception as e:
        log("webhook 推送失败: {!r}".format(e))


def send_trending_note(bot=None, force=False, source="scheduled"):
    """执行一次完整发送。返回结果字符串（首字符 ✅/⚠️/❌ 供 trigger 判状态）。"""
    def done(msg, push=True):
        log(msg)
        if push:
            _push(msg, source)
        return msg

    try:
        if not config.ENABLED:
            return done("已禁用（config.ENABLED=False），跳过", push=False)
        if not force and _already_sent_today():
            return done("今天已发过，跳过（防重）", push=False)

        # 顺序保证：AI 日报先发，GitHub 趋势后发。没轮到就返回 ⚠️，
        # 由 trigger 安排重试（不是失败，等一会儿它自己就好了）。
        if config.WAIT_FOR_AI_NEWS and not force and not _ai_news_sent_today():
            return done("⚠️ AI 日报今天还没发出去，先让它发，本次跳过等重试", push=False)

        if not os.path.exists(config.DATA_FILE):
            return done("⚠️ 数据文件不存在：{}，跳过".format(config.DATA_FILE))
        with open(config.DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)

        today = datetime.date.today().isoformat()
        if d.get("date") != today:
            return done("⚠️ 今天（{}）的 GitHub 趋势还没就位，当前数据日期：{}，未发送。"
                        "（等 mac-mini 07:30 的推送、或检查推送链路）"
                        .format(today, d.get("date") or "无"))
        if not force and not _fresh_enough(d):
            return done("⚠️ 数据过旧(>{}h)，跳过".format(config.MAX_AGE_HOURS))

        n = count_items(d)
        if n < 3:
            return done("⚠️ 只有 {} 个项目，判为异常，跳过".format(n))

        title, frag, hits = render(d)
        cf_bytes = _build_cf_html(frag)
        _clip_html(cf_bytes, title)
        # 选笔记必须用当日完整标题（含日期）。用前缀模糊匹配会把历史笔记翻出来发，
        # ai_news_note 实测栽过一次（把 7/23 的旧日报发进群）。
        title_kw = title.replace("📈", "").strip()
        if hits:
            log("内容审查：已打码敏感词 {}".format(hits))
        log("开始发送趋势笔记：{} -> {}（{} 个项目）".format(title, config.TARGET, n))

        ok, why = _desktop_usable()
        if not ok:
            return done("❌ 发送失败（环境）：{}".format(why))

        _close_update_windows()
        ok, why = _close_all_editors()
        if not ok:
            return done("❌ 发送失败（环境）：{}".format(why))

        ok, msg = _create_note_from_clipboard(cf_bytes, title, title_kw)
        if not ok:
            return done("❌ 发送失败（建笔记）：{}".format(msg))

        cell = None
        for _ in range(10):
            time.sleep(1.0)
            cell = _find_note_cell(title_kw)
            if cell:
                break
        if not cell:
            return done("❌ 发送失败（建笔记）：收藏里没出现「{}」，"
                        "多半是粘贴没落进编辑器，中止发送".format(title_kw))
        log("已确认新笔记入库：{}".format(title_kw))

        ok, win, msg = _open_target(config.TARGET)
        if not ok:
            return done("❌ 发送失败（打开目标）：{}".format(msg))
        log("已打开目标：{}".format(msg))

        ok, msg = _send_favorite(win, title_kw)
        if not ok:
            return done("❌ 发送失败（发送收藏）：{}".format(msg))

        _mark_sent_today()
        extra = "，已过滤敏感词：{}".format("、".join(hits)) if hits else ""
        return done("✅ GitHub 趋势已发送到 {}（{} 个项目）{}".format(config.TARGET, n, extra))
    except Exception as e:
        import traceback
        log(traceback.format_exc())
        return done("❌ 发送异常：{}".format(e))
    finally:
        _drop_topmost()
