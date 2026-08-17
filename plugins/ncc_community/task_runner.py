# -*- coding: utf-8 -*-
"""后台任务触发器 —— 让运维指令不必真有人在管理群里发。

为什么要它：所有微信操作**必须在 bot 进程内**跑（独立进程会跟主循环每 3-5 秒的
消息轮询抢微信主窗口，AI 日报断供两天就是这么来的），而 bot 进程平时只听微信消息。
于是想跑一条「修备注 全部」这种长指令，就得有人拿手机在管理群里发——
排查和回归时很不方便。

照搬 ai_news_note/trigger.py 那套：外部（mac 侧走 SMB 挂载 / SSH）往
`data/task_request.txt` 写一行指令，bot 在自己的 schedule 里每 10 秒消费一次，
起后台线程执行，把本该发到管理群的每条回复追加进 `data/task_result.txt`。

安全边界：
- 只认 `forward._try_direct_command` 那套【直接文本指令】，不进转发状态机——
  意味着它发不出群发消息，最坏情况是白跑一次体检。
- 请求文件超过 STALE_SECONDS 当陈旧丢弃，避免 bot 没在跑时攒下的请求隔几小时诈尸。
- 执行期间举起 `set_forwarding` 闸门，主循环整轮让路，跟群发同款，不抢窗口。
- 同时只跑一个任务，上一个没跑完的直接忽略。
"""
from __future__ import annotations

import os
import threading
import time
import traceback

from . import store
from .common import log
from .wxlock import keepalive, set_forwarding

_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")
REQUEST_PATH = os.path.join(DATA_DIR, "task_request.txt")
RESULT_PATH = os.path.join(DATA_DIR, "task_result.txt")

STALE_SECONDS = 30 * 60      # 请求超过这么久没被消费就丢弃
_SENDER = "__task_runner__"  # 伪 sender，指令状态机按它隔离，不会串到真人

_RUNNING = threading.Lock()


class FileSink:
    """冒充一个 chat：`common.reply` 只会调 `SendMsg(msg=...)`，
    这里把内容逐条追加到结果文件，于是所有指令一行不改就能在无人值守下跑。"""

    def __init__(self, path: str):
        self.path = path

    def SendMsg(self, msg=None, who=None, **kwargs):
        text = msg if msg is not None else ""
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n\n")
        except OSError as e:
            log("ERROR", f"后台任务结果写盘失败：{e}")
        return True


def register(bot, schedule) -> None:
    """挂进 bot 的 schedule。由 wxbot_core 在定时任务注册处调用。"""
    schedule.clear("ncc_task_runner")
    schedule.every(10).seconds.do(tick, bot).tag("ncc_task_runner")
    # 主循环只在有定时任务开关打开时才 run_pending，这个标志让它把我们也算上
    bot._ncc_task_runner_enabled = True
    log("INFO", f"后台任务触发器已挂载：写 {REQUEST_PATH} 即执行")


def _read_request():
    """读并【立即删除】请求文件。返回 (指令文本, 距写入的秒数)，没有请求返回 (None, 0)。
    先删再执行：崩了也不会开机重放，宁可少跑一次。"""
    if not os.path.exists(REQUEST_PATH):
        return None, 0.0
    text, age = "", 0.0
    try:
        age = time.time() - os.path.getmtime(REQUEST_PATH)
        # utf-8-sig：Windows PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM，
        # 读成普通 utf-8 时指令头上多个不可见的 ﻿，指令表怎么也匹配不上
        # （2026-08-15 实测：回「不认识的指令：检查群组 全部」，肉眼完全看不出差别）。
        with open(REQUEST_PATH, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    except OSError as e:
        log("WARNING", f"读后台任务请求失败：{e}")
    finally:
        try:
            os.remove(REQUEST_PATH)
        except OSError:
            pass
    return (text or None), age


def tick(bot) -> None:
    """schedule 回调：每 10 秒看一眼有没有外部请求。"""
    try:
        text, age = _read_request()
        if not text:
            return
        if age > STALE_SECONDS:
            log("WARNING", f"后台任务请求「{text}」已陈旧（{age / 60:.0f} 分钟前），丢弃")
            return
        if not _RUNNING.acquire(blocking=False):
            log("WARNING", f"上一个后台任务还在跑，忽略「{text}」")
            return
        try:
            threading.Thread(target=_guarded_run, args=(bot, text),
                             name="ncc-task-runner", daemon=True).start()
        except Exception:
            _RUNNING.release()
            raise
    except Exception as e:
        log("ERROR", f"后台任务触发失败：{e}")


def _guarded_run(bot, text: str) -> None:
    """跑完必须把"同时只跑一个"的闸放掉——release 单独放这里，
    好让 _run 本身能被直接调用（单测/人工触发）。"""
    try:
        _run(bot, text)
    finally:
        _RUNNING.release()


def _run(bot, text: str) -> None:
    from . import forward

    sink = FileSink(RESULT_PATH)
    try:
        with open(RESULT_PATH, "w", encoding="utf-8") as f:
            f.write(f"=== 后台任务「{text}」开始 "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n\n")
    except OSError as e:
        log("ERROR", f"后台任务结果文件初始化失败：{e}")

    log("INFO", f"后台任务开始：{text}")
    set_forwarding(True)       # 主循环让路，别跟我抢主窗口
    # ★ 闸门会在久无续期后自动失效（_MAX_HOLD=300 秒，防的是"任务线程死了却把闸门
    # 永远举着"）。可「查寻址 全部」「检查群组 全部」「修备注 全部」这类任务动辄跑
    # 5~10 分钟，闸门一到点就过期 → 主循环恢复 → listen_health 探针照常触发
    # AddListenChat → 开出独立聊天窗口抢走前台 → 后面每一次搜索都报
    # Find Control Timeout。2026-08-13 实测：查寻址跑了 571 秒，前 5 分钟一切正常，
    # 之后 10 个群成片失败，而它们上一轮全是通过的。
    # 所以在这儿起一根心跳去续期：任务活着闸门就一直举着，任务一结束立刻落闸。
    stop_beat = threading.Event()

    def _beat():
        while not stop_beat.wait(30):
            keepalive()

    beat = threading.Thread(target=_beat, name="ncc-task-keepalive", daemon=True)
    beat.start()
    t0 = time.time()
    try:
        handled = forward._try_direct_command(bot, sink, store.load(), _SENDER, text)
        if not handled:
            sink.SendMsg(msg=f"不认识的指令「{text}」。支持：扫群 / 查新群 / 查寻址 全部 / "
                             f"修备注 预览 / 修备注 全部 / 核对备注 全部 / 检查群组 全部 / 同步 / 待归类")
    except Exception as e:
        sink.SendMsg(msg=f"执行出错：{e}\n{traceback.format_exc()}")
        log("ERROR", f"后台任务「{text}」出错：{e}")
    finally:
        stop_beat.set()
        set_forwarding(False)
        sink.SendMsg(msg=f"=== 任务结束，耗时 {time.time() - t0:.1f} 秒 ===")
        log("INFO", f"后台任务结束：{text}（{time.time() - t0:.1f}s）")
