# -*- coding: utf-8 -*-
"""外部触发 + 失败重试。两者都在 bot 主循环的 schedule 里跑，绝不另起进程。

【为什么不再用 Windows 计划任务 ai_news_send_now 起独立进程】2026-08-03 血泪：
sender.py 开头那句"本模块在 bot 进程内、由 schedule 主循环单线程调用，和 bot 其它
UI 操作串行，不会和 bot 抢微信 UI"是**硬前提，不是描述**。独立进程会和 bot 每 3-5 秒
的消息轮询抢微信主窗口 —— 实测 18:44：

    18:44:57 [日报]  笔记编辑器 hwnd=27329184 ...          编辑器置前成功
    18:44:58 [wxauto] 更新当前聊天窗口缓存信息：Sigma∑_🐕   bot 把主窗口拉了回去
    18:45:00 [日报]  前台=主窗口 点归属编辑器=False         Ctrl+V 打在主窗口上

连败 3 次。而 bot 本来就跑在交互桌面 session 2 —— 当初搞计划任务只是因为 SSH 落在
session 0 碰不到桌面，跳板要的是"进入 session 2"，不是"新开进程"。所以现在 mac-mini
只写一个请求文件（写文件与会话无关），由本模块在 schedule 循环里消费，天然串行。

【重试】原来一次不成整天放弃：8/3 早上 07:04 / 07:05 / 09:30 三次全挂，日报当天断供，
而同一天 18:54 手动跑一次就过了 —— 说明那个抢焦点故障是间歇性的，隔一段再试就有救。
现在失败后隔 RETRY_DELAY_MIN 分钟自动重试，最多 RETRY_MAX 次。
重试一律不带 force，靠 sender 自己的 last_sent.txt 防重兜底，不可能重复发群。
"""
import os
import json
import time
import datetime

from . import config
from .sender import send_daily_note_guarded, log

_DIR = os.path.dirname(config.DATA_FILE)
# mac-mini 通过 SSH 写这个文件表示"请发今天的日报"；本模块消费后立刻删掉。
REQUEST_FILE = os.path.join(_DIR, "send_request.flag")
# 结果回写给 mac-mini 读（格式与旧的 ai_news_send_now.py 完全一致，触发脚本不用改解析）。
RESULT_FILE = os.path.join(_DIR, "send_result.json")

# 请求文件超过这个岁数就当陈旧丢弃：bot 当时可能没在跑（重启/停用），
# 别在几小时后突然把一个早已放弃的请求执行掉。
REQUEST_MAX_AGE_SEC = 600

RETRY_DELAY_MIN = 15   # 失败后隔多久重试
RETRY_MAX = 4          # 最多重试几次（约覆盖 1 小时）

# 重试状态（只在 schedule 单线程里读写，不需要锁）。
# 存在内存里：整进程重启（改代码 / ui_watchdog 拉起）会丢掉待重试状态。
# 可以接受 —— 兜底还有 mac-mini 那边的触发和每日定时任务，且丢失只会少发一次重试，
# 不会重复发群（sender 有 last_sent.txt 防重）。
_retry_at = None       # datetime，到点重试
_retry_left = 0


def _status_of(text):
    """把 sender 的 ✅/⚠️/❌ 文案翻译成 mac 端可判的 status（沿用旧入口的约定）。"""
    if text.startswith("✅"):
        return "ok"
    if text.startswith("❌"):
        return "failed"
    return "skipped"   # ⚠️ 数据没就位 / 今天已发过 / 已禁用


def _write_result(status, text):
    """原子写结果文件，避免 mac 端读到写了一半的内容。"""
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
        log(f"写 send_result.json 失败（不影响发送）：{e!r}")


def _arm_retry(status, text):
    """决定要不要安排下一次重试。

    不重试的情况：发成功了、今天已发过（防重）、插件被禁用 —— 这三种再试也没意义。
    其余（真失败 / 数据没就位 / 数据过旧）都值得隔一会儿再来一次：mac-mini 推迟到了、
    或者那个间歇性的抢焦点故障过去了，都能自己好。
    """
    global _retry_at, _retry_left
    if status == "ok" or "已发过" in text or "已禁用" in text:
        _retry_at, _retry_left = None, 0
        return
    if _retry_left <= 0:
        _retry_at = None
        log(f"重试次数已用尽（共 {RETRY_MAX} 次），今天不再自动重试")
        return
    # 预算只在真正安排重试时扣：首次尝试不算一次"重试"，
    # 否则 RETRY_MAX=4 实际只会重试 3 次。
    _retry_left -= 1
    _retry_at = datetime.datetime.now() + datetime.timedelta(minutes=RETRY_DELAY_MIN)
    log(f"本次未成功，{RETRY_DELAY_MIN} 分钟后重试（此后还剩 {_retry_left} 次）：{_retry_at:%H:%M}")


def run_once(bot, source):
    """跑一次发送并回写结果 + 安排重试。只应由 schedule 循环调用（保证串行）。"""
    try:
        # 走 guarded 入口：笔记流程一行不改，只在它失败、且确定群里还没收到任何东西时，
        # 才追加一次「不新建窗口」的纯文本降级发送（见 sender.py 末尾）。
        text = str(send_daily_note_guarded(bot, force=False, source=source))
    except Exception as e:
        import traceback
        text = f"❌ 发送异常：{e}"
        log(f"发送抛异常：{e!r}\n{traceback.format_exc()}")
    status = _status_of(text)
    _write_result(status, text)
    _arm_retry(status, text)
    return text


def start_day(bot, source="scheduled"):
    """每日定时任务入口：重置重试预算，然后跑一次。"""
    global _retry_left, _retry_at
    _retry_left, _retry_at = RETRY_MAX, None
    return run_once(bot, source)


def _take_request():
    """有新鲜的外部请求就消费掉并返回 True。文件一律先删再执行，避免重复触发。"""
    try:
        if not os.path.exists(REQUEST_FILE):
            return False
        age = time.time() - os.path.getmtime(REQUEST_FILE)
        os.remove(REQUEST_FILE)
        if age > REQUEST_MAX_AGE_SEC:
            log(f"忽略陈旧的发送请求（{int(age)}s 前写的，超过 {REQUEST_MAX_AGE_SEC}s），已删除")
            return False
        return True
    except Exception as e:
        log(f"读取发送请求失败：{e!r}")
        return False


def tick(bot):
    """schedule 每 10 秒调一次：先看有没有外部请求，再看重试到点没有。

    整个函数跑在 bot 主循环里，和消息轮询串行 —— 这正是它存在的意义，别挪到线程里去。
    """
    global _retry_at, _retry_left
    try:
        if _take_request():
            log("收到 mac-mini 的发送请求，在 bot 进程内执行")
            _retry_left = RETRY_MAX      # 外部触发也给一轮重试预算
            _retry_at = None
            run_once(bot, "mac-trigger")
            return
        if _retry_at and datetime.datetime.now() >= _retry_at:
            _retry_at = None
            log("到点重试发送日报")
            run_once(bot, "retry")
    except Exception as e:
        # tick 每 10 秒跑一次，绝不能因为它把 bot 主循环搞挂
        log(f"tick 出错（已忽略）：{e!r}")
