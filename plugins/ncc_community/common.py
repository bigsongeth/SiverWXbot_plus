# -*- coding: utf-8 -*-
"""ncc_community 插件公共工具：日志与统一回复。"""
from __future__ import annotations

# 机器人程序化回复的统一前缀。
# 指令解析层会忽略带此前缀的消息，保证机器人自己的回复（在管理群里
# 属于 self 消息，会重新进入回调）不会被当成指令二次处理。
REPLY_PREFIX = "🤖"

try:
    from logger import log as _log
except Exception:  # 单测环境没有项目根的 logger
    def _log(level="INFO", message=""):
        print(f"[{level}] {message}")


def log(level: str, message: str) -> None:
    try:
        _log(level=level, message=f"[ncc_community] {message}")
    except Exception:
        pass


def reply(chat, text: str):
    """在会话内发送带前缀的机器人回复，失败只记日志不抛出。"""
    try:
        return chat.SendMsg(msg=f"{REPLY_PREFIX} {text}")
    except Exception as e:
        log("ERROR", f"回复失败: {e}")
        return None


def notify_admin(bot, cfg, text: str) -> None:
    """给管理群推一条带前缀的提醒（没配管理群或发送失败只记日志，不抛出）。

    用于"用户侧已经回过话、但需要人接手"的场景（拉群失败、发现新群…），
    避免用户在私聊里干等着问"管理员在哪啊"。"""
    try:
        admin = (cfg or {}).get("admin_group")
    except Exception:
        admin = None
    if not admin:
        return
    try:
        bot.wx.SendMsg(msg=f"{REPLY_PREFIX} {text}", who=admin)
    except Exception as e:
        log("ERROR", f"提醒管理群失败: {e}")


def is_bot_reply(text: str) -> bool:
    return (text or "").strip().startswith(REPLY_PREFIX)
