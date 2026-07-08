# -*- coding: utf-8 -*-
"""微信主窗口全局访问锁 —— 串行化"谁在操作主窗口"。

背景：wxauto 是 UI 自动化，整个微信只有一个主窗口。机器人主循环（单线程）会跑
消息轮询(GetNextNewMessage)和新好友检查(SwitchToContact)，而 ncc 转发跑在后台
线程也要驱动主窗口——两者并发就会互相把窗口切走，导致"转发到一半失败"。

用这一把 RLock 把三方（转发线程 / 主循环消息轮询 / 主循环新好友检查）串行化：
谁拿到锁谁操作，另一方等或跳过。RLock 便于同线程内嵌套获取不自锁。

⚠️ wxbot_core.py 的主循环里有两处 hook 引用了它（见 AI_COLLABORATION_GUIDE.md
"潜在冲突：主窗口串行锁"一节），合并上游时务必保留。
"""
import threading

WX_LOCK = threading.RLock()
