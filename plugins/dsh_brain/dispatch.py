# -*- coding: utf-8 -*-
"""并发回复调度器：同一会话串行、不同会话并行（2026-09-08）。

为什么要有它：改造前一条消息从收到到回复完，整个过程占着 wxautox 那唯一一个监听线程
（`WxParam.LISTENER_EXCUTOR_WORKERS = 1`），而问大脑一轮中位 57 秒、p90 177 秒 ——
这段时间别的群一条消息都读不到。生产日志里有活标本：2026-09-06 13:16:43 收到一条消息，
撞上 DusAPI 504 重试链，到 13:21:58 才回复，中间 5 分钟整个机器人是聋的。

怎么做：命中大脑的会话，把「问大脑 + 发送」整条挪到 worker 线程，监听线程立刻返回继续轮询。
★ worker 里回调的还是 `WXBot.process_message` 本身（靠线程标记防止再次派发），
  所以关键词回复 / 图片识别 / 历史 / 分条 / 接话闸门 / 故障转移一行都不用重写 ——
  **别改成在这里重新实现一遍发送逻辑**，那些分支是一堆线上事故换来的。

保序：每个会话一个队列 + 至多一个在跑的 worker，所以同一会话严格按到达顺序处理；
不同会话之间才并行。用户 2026-09-08 拍板「并发回复」，不做「群内串行、跨群并发」——
群里多人同时 @ 就是并发处理、谁先好谁先发。

关掉的办法：config.json 里 `async_reply: false`（默认就是关的），立刻退回同步行为。
设计见 docs/superpowers/specs/2026-09-08-concurrency-design.md §3。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Deque, Dict, Optional

from . import brain_enabled, store

_local = threading.local()

_LOCK = threading.Lock()
_queues: Dict[str, Deque] = {}      # 会话 -> 待处理任务队列
_busy: set = set()                  # 正在被某个 worker 排空的会话
_workers: Dict[str, threading.Thread] = {}
_stats = {"submitted": 0, "done": 0, "dropped": 0, "errors": 0, "failed": 0}


def _log(level: str, msg: str) -> None:
    try:
        from logger import log
        log(level=level, message=f"[dsh_brain.dispatch] {msg}")
    except Exception:
        pass


def in_worker() -> bool:
    """当前线程是不是本模块的 worker。派发入口靠它防止无限套娃。"""
    return bool(getattr(_local, "in_worker", False))


def async_enabled() -> bool:
    cfg = store.load()
    return bool(cfg.get("enabled")) and bool(cfg.get("async_reply"))


def _max_workers() -> int:
    return max(1, int(store.load().get("max_workers") or 1))


def _queue_max() -> int:
    return max(1, int(store.load().get("queue_max_per_conv") or 1))


def is_group_chat(bot, chat) -> bool:
    """群聊判定：配置里的群名，或子窗口自己报的 chat_type。两条都要看 ——
    全局模式下的群走 chat_type，白名单模式下的群走 config.group。"""
    who = str(getattr(chat, "who", "") or "")
    try:
        if who in (bot.config.group or []):
            return True
    except Exception:
        pass
    return str(getattr(chat, "chat_type", "") or "") == "group"


def maybe_dispatch(bot, chat, message) -> bool:
    """够条件就把这条消息交给 worker，返回 True 表示调用方可以立刻收工。

    任何一步不满足都返回 False，调用方照旧同步处理 —— 这个函数永远不能让消息消失。
    """
    if in_worker():
        return False                      # 已经在 worker 里了，正常往下走
    if not async_enabled():
        return False
    who = str(getattr(chat, "who", "") or "")
    if not who:
        return False
    if not brain_enabled(who, is_group_chat(bot, chat)):
        return False                      # 不走大脑的会话（老接口很快）不值得挪线程
    return submit(who, lambda: bot.process_message(chat, message), who)


def submit(conv: str, task: Callable[[], object], label: str = "") -> bool:
    """把任务排进该会话的队列；需要时起一个 worker 去排空它。"""
    with _LOCK:
        q = _queues.setdefault(conv, deque())
        if len(q) >= _queue_max():
            # 积压说明大脑跟不上这个会话的说话速度。丢最旧的那条：群聊里 5 分钟前的问题
            # 早就过时了，回它不如回最新的。丢弃留痕，别静默。
            q.popleft()
            _stats["dropped"] += 1
            _log("WARNING", f"{conv} 待处理队列已满（{_queue_max()}），丢掉最旧的一条")
        q.append(task)
        _stats["submitted"] += 1
        if conv in _busy:
            return True                   # 已有 worker 在排空这个会话，它会顺手取走
        if len(_busy) >= _max_workers():
            # 名额满了：不新起 worker，但任务留在队列里。等某个 worker 收工时会来捡
            return True
        _busy.add(conv)
        th = threading.Thread(target=_drain, args=(conv,), name=f"brain-{conv[:12]}", daemon=True)
        _workers[conv] = th
    th.start()
    return True


def _drain(conv: str) -> None:
    """排空一个会话的队列。同一会话同时只有一个 worker，所以这里天然保序。"""
    _local.in_worker = True
    try:
        while True:
            with _LOCK:
                q = _queues.get(conv)
                if not q:
                    _busy.discard(conv)
                    _workers.pop(conv, None)
                    break
                task = q.popleft()
            try:
                # ncc 转发进行时先让路，别跟它抢微信主窗口（同 message_handle_callback 的做法）
                try:
                    from plugins.ncc_community.wxlock import wait_while_forwarding
                    wait_while_forwarding()
                except Exception:
                    pass
                res = task()
                with _LOCK:
                    _stats["done"] += 1
                if res is not None and not res:
                    # 同步路径下这里会走 message_handle_callback 的 is_err 告警（wxbot_core.py:3491），
                    # 异步之后那条路径拿到的是「已派发」的 True，真正的失败只有这里看得见 —— 别静默。
                    with _LOCK:
                        _stats["failed"] += 1
                    _log("ERROR", f"{conv} 消息处理返回失败：{res!r}")
            except Exception as e:
                # 一条消息处理失败绝不能带崩 worker，否则这个会话后面的全卡在队列里
                with _LOCK:
                    _stats["errors"] += 1
                _log("ERROR", f"{conv} 处理消息出错：{type(e).__name__}: {e}")
    finally:
        _local.in_worker = False
        _pick_up_waiting()


def _pick_up_waiting() -> None:
    """有 worker 收工后，把因为名额不够而干等着的会话捡起来。

    不做这一步的话，submit 时名额满的那些会话会一直没人管 —— 队列里有任务、却没有 worker，
    要等到该会话下一条消息进来才被唤醒（那条消息可能永远不来）。
    """
    start = []
    with _LOCK:
        for conv, q in _queues.items():
            if not q or conv in _busy:
                continue
            if len(_busy) >= _max_workers():
                break
            _busy.add(conv)
            th = threading.Thread(target=_drain, args=(conv,), name=f"brain-{conv[:12]}", daemon=True)
            _workers[conv] = th
            start.append(th)
    for th in start:
        th.start()


def stats() -> dict:
    with _LOCK:
        return dict(_stats, busy=len(_busy), queued=sum(len(q) for q in _queues.values()),
                    conversations=len([c for c, q in _queues.items() if q]))


def reset_for_test() -> None:
    """单测用：清空全局状态。生产不调。"""
    with _LOCK:
        _queues.clear()
        _busy.clear()
        _workers.clear()
        for k in _stats:
            _stats[k] = 0
