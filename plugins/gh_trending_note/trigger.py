# -*- coding: utf-8 -*-
"""外部触发 + 失败重试。结构照搬 ai_news_note.trigger（同样的坑、同样的解法）。

两者都在 bot 主循环的 schedule 里跑，绝不另起进程 —— 独立进程会和 bot 的消息轮询
抢微信主窗口，Ctrl+V 会打到聊天框里去。详见 ai_news_note/trigger.py 顶部那段血泪。

比 ai_news_note 多一种"值得重试"的情况：AI 日报还没发完（WAIT_FOR_AI_NEWS），
这时返回 ⚠️，隔 RETRY_DELAY_MIN 分钟再来 —— 等它发完就轮到我们了。
"""
import os
import json
import time
import datetime

from . import config
from .sender import send_trending_note, log, ai_news_sent_at, already_sent_today

_DIR = os.path.dirname(config.DATA_FILE)
REQUEST_FILE = os.path.join(_DIR, "send_request.flag")
RESULT_FILE = os.path.join(_DIR, "send_result.json")

REQUEST_MAX_AGE_SEC = 600
RETRY_DELAY_MIN = 15
RETRY_MAX = 4

_retry_at = None
_retry_left = 0
# 跟随模式每天只放行一次。否则发送失败后 already_sent_today() 仍是 False，
# 10 秒后的下一个 tick 会再次触发，变成每 10 秒重试一轮的疯跑。
# 失败后交给 _arm_retry 的 15 分钟退避，别在这里绕过它。
_followed_on = None


def _status_of(text):
    if text.startswith("✅"):
        return "ok"
    if text.startswith("❌"):
        return "failed"
    return "skipped"


def _write_result(status, text):
    payload = {
        "status": status,
        "result": str(text),
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": datetime.date.today().isoformat(),
    }
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = RESULT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RESULT_FILE)
    except Exception as e:
        log("写 send_result.json 失败（不影响发送）：{!r}".format(e))


def _arm_retry(status, text):
    global _retry_at, _retry_left
    if status == "ok" or "已发过" in text or "已禁用" in text:
        _retry_at, _retry_left = None, 0
        return
    if _retry_left <= 0:
        _retry_at = None
        log("重试次数已用尽（共 {} 次），今天不再自动重试".format(RETRY_MAX))
        return
    _retry_left -= 1
    _retry_at = datetime.datetime.now() + datetime.timedelta(minutes=RETRY_DELAY_MIN)
    log("本次未成功，{} 分钟后重试（此后还剩 {} 次）：{:%H:%M}".format(
        RETRY_DELAY_MIN, _retry_left, _retry_at))


def run_once(bot, source):
    try:
        text = str(send_trending_note(bot, force=False, source=source))
    except Exception as e:
        import traceback
        text = "❌ 发送异常：{}".format(e)
        log("发送抛异常：{!r}\n{}".format(e, traceback.format_exc()))
    status = _status_of(text)
    _write_result(status, text)
    _arm_retry(status, text)
    return text


def start_day(bot, source="scheduled"):
    global _retry_left, _retry_at
    _retry_left, _retry_at = RETRY_MAX, None
    return run_once(bot, source)


def _take_request():
    try:
        if not os.path.exists(REQUEST_FILE):
            return False
        age = time.time() - os.path.getmtime(REQUEST_FILE)
        os.remove(REQUEST_FILE)
        if age > REQUEST_MAX_AGE_SEC:
            log("忽略陈旧的发送请求（{}s 前写的），已删除".format(int(age)))
            return False
        return True
    except Exception as e:
        log("读取发送请求失败：{!r}".format(e))
        return False


def _in_follow_window(now=None):
    """现在是不是允许跟随触发的时段。配错或没配就放行（不因为配置问题挡住正常发送）。"""
    win = getattr(config, "FOLLOW_WINDOW", None)
    if not win:
        return True
    try:
        start, end = [datetime.datetime.strptime(s, "%H:%M").time() for s in win]
    except Exception:
        return True
    t = (now or datetime.datetime.now()).time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end   # 跨午夜的窗口（如 22:00-02:00）


def _should_follow():
    """AI 日报刚发完、该我接上了吗？（跟随模式）

    这样群里两条日报是连着出现的，而不是一条 09:30 一条死等 09:40。
    SEND_TIME 仍在，只是退化成兜底：跟随没触发时到点照发，防重保证不会发两次。
    """
    if not getattr(config, "FOLLOW_AI_NEWS", False):
        return False
    if _followed_on == datetime.date.today():   # 今天已经跟随过一次
        return False
    if not _in_follow_window():
        return False
    if already_sent_today():
        return False
    at = ai_news_sent_at()
    if not at:
        return False
    return (time.time() - at) >= getattr(config, "FOLLOW_DELAY_SEC", 90)


def tick(bot):
    global _retry_at, _retry_left, _followed_on
    try:
        if _should_follow():
            waited = int(time.time() - ai_news_sent_at())
            _followed_on = datetime.date.today()   # 先落闸再执行，失败也不重入
            log("AI 日报已发完 {} 秒，接着发 GitHub 趋势（跟随模式）".format(waited))
            _retry_left = RETRY_MAX
            _retry_at = None
            run_once(bot, "follow-ai-news")
            return
        if _take_request():
            log("收到外部发送请求，在 bot 进程内执行")
            _retry_left = RETRY_MAX
            _retry_at = None
            run_once(bot, "external-trigger")
            return
        if _retry_at and datetime.datetime.now() >= _retry_at:
            _retry_at = None
            log("到点重试发送 GitHub 趋势")
            run_once(bot, "retry")
    except Exception as e:
        log("tick 出错（已忽略）：{!r}".format(e))
