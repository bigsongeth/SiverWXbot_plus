# -*- coding: utf-8 -*-
"""微信主窗口全局访问闸门 —— "转发进行时，其它任务排队让路"。

背景：wxauto 是 UI 自动化，整个微信只有一个主窗口，且有多条线在驱动它：
  - wxautox 监听线程 `message_handle_callback`（群/私聊消息处理、AI 回复、转发触发）
  - 主循环 `ALLListen_mode`（动态新会话轮询）+ 新好友检查（SwitchToContact）+ 定时任务
  - ncc 转发后台线程
它们并发就会互相把窗口切走，导致"转发到一半失败"。

用户要求（2026-07）：进了转发就先把转发做完，其它（加群/加好友/朋友圈/AI回复…）
排队等它做完再按序执行，别互相堵。

机制：
  - 转发线程开工时 set_forwarding(True) 举起"转发中"闸门，收工 finally 里 False。
  - 主循环每轮先看闸门：转发中就跳过本轮所有窗口活儿（只心跳+睡，消息留在微信里没读，
    转发完下一轮自然读到，不丢）。
  - 监听线程回调进来先 wait_while_forwarding()：转发中就等，等它落闸再处理（消息不丢，
    wxauto 会把后续回调排队，做完按序处理）。
  - WX_LOCK 仍保留，给转发各步与偶发的在途回调做一层串行兜底。

⚠️ wxbot_core.py 主循环 + message_handle_callback 里有引用 hook（见
AI_COLLABORATION_GUIDE.md「潜在冲突：主窗口串行闸门」），合并上游务必保留。
"""
import threading
import time

WX_LOCK = threading.RLock()

_FLAG_LOCK = threading.Lock()
_forwarding = False
_started_at = 0.0
# 闸门最长有效期（秒）：万一转发线程异常没落闸，超过这个时间闸门自动失效，
# 避免主循环/监听线程被永久挡住。取一个远大于任何正常群发时长的值。
_MAX_HOLD = 1800.0
# 监听回调等待闸门的最长秒数，超时就放行处理（宁可偶发一次并发，不永久卡住收消息）
_WAIT_CAP = 1200.0


def set_forwarding(value: bool) -> None:
    global _forwarding, _started_at
    with _FLAG_LOCK:
        _forwarding = bool(value)
        _started_at = time.time() if value else 0.0


def is_forwarding() -> bool:
    with _FLAG_LOCK:
        if not _forwarding:
            return False
        if time.time() - _started_at > _MAX_HOLD:   # 闸门过期兜底
            return False
        return True


def wait_while_forwarding(poll: float = 0.5) -> None:
    """转发进行中就阻塞等待，直到落闸或超过 _WAIT_CAP。"""
    waited = 0.0
    while is_forwarding() and waited < _WAIT_CAP:
        time.sleep(poll)
        waited += poll
