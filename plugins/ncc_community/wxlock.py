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
# 闸门最长有效期（秒）——注意它量的是【距上次续期】而不是【整轮转发时长】。
#
# 2026-08-04 血泪：原来这里是 1800 秒、监听侧另有 1200 秒的等待上限，都是按
# "一轮转发不会超过这么久"拍的。实际全量 105 个群、每群 2.5~4.5 秒加每 10 群歇
# 5~9 秒，【一条消息】就要 6~8 分钟，两三条就顶穿两个阈值 —— 闸门自己失效、
# 监听线程等腻了放行，于是转发跑到一半，主循环和监听线程回来抢主窗口，
# 表现就是"转发莫名其妙被打断、报一堆说不清的错"。
#
# 现在改成转发线程每转完一个群 keepalive() 续一次期，闸门只在转发线程【真的死了】
# （连续 _MAX_HOLD 秒没有任何进展）才过期。单群最坏情况 = 转发调用超时 + 群间延迟，
# 300 秒足够宽松。
_MAX_HOLD = 300.0


def set_forwarding(value: bool) -> None:
    global _forwarding, _started_at
    with _FLAG_LOCK:
        _forwarding = bool(value)
        _started_at = time.time() if value else 0.0


def keepalive() -> None:
    """转发仍在推进 —— 续期闸门。转发线程每完成一个群调一次。"""
    global _started_at
    with _FLAG_LOCK:
        if _forwarding:
            _started_at = time.time()


def is_forwarding() -> bool:
    with _FLAG_LOCK:
        if not _forwarding:
            return False
        if time.time() - _started_at > _MAX_HOLD:   # 久无续期 = 转发线程死了，放行
            return False
        return True


def wait_while_forwarding(poll: float = 0.5) -> None:
    """转发进行中就阻塞等待，直到落闸。

    不再另设等待上限：闸门自己带"久无续期即失效"的兜底（_MAX_HOLD），
    再叠一层固定上限只会让长转发跑到一半被人抢窗口。"""
    while is_forwarding():
        time.sleep(poll)
