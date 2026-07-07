# -*- coding: utf-8 -*-
"""ncc_community 插件：NCC 社群自动化。

- forward.py   管理群转发：菜单式指令，把任意消息群发到一组群聊（读 registry）
- registry.py  本地群登记表：群/分组/权限，Notion 同步下来的运行时缓存
- notion_sync  Notion 双向同步：拉分组/权限、回写新发现群
- remark.py    干净打🐶备注（登记表标志幂等，避免 wxautox 追加坑）
- discovery.py 被动发现：未登记群一说话 → 打备注 + 入 Notion 待归类 + 提醒
- welcome.py   分群迎新：新人进群发欢迎语 + 在地化链接卡片
- invite.py    关键词拉群：私聊/群聊命中关键词后把发送人拉进目标群

权限模型：管理群（进群即管理员）替代旧协议的 wxid 白名单验证。
wxbot_core.py 只保留三个最小 hook（friend/self/system 三个分支各一处），
业务逻辑与配置全部收在本目录，避免与上游合并冲突。
"""
from __future__ import annotations

from . import store
from .forward import handle_admin_message
from .invite import handle_invite
from .welcome import handle_welcome


def handle_friend_message(bot, chat, msg) -> bool:
    """friend 消息入口：管理群走指令/转发；其他群旁路发现新群；私聊/群走拉群关键词。

    返回 True 表示已处理，核心应跳过后续 AI/转发流程。
    """
    cfg = store.load()
    who = str(getattr(chat, "who", "") or "")
    if who == cfg.get("admin_group"):
        if _try_batch(bot, chat, msg, cfg):
            return True
        return handle_admin_message(bot, chat, msg, cfg)

    # 旁路：被动发现新群（不拦截正常流程，异常不外抛）
    try:
        from . import discovery
        discovery.handle_discovery(bot, chat, msg, cfg)
    except Exception:
        pass

    return handle_invite(bot, chat, msg, cfg)


def handle_self_message(bot, chat, msg) -> bool:
    """self 消息入口：机器人账号自己在管理群发的消息也当指令处理。

    场景：用手机登录机器人微信号直接在管理群里发指令。
    机器人的程序化回复带 REPLY_PREFIX 前缀，会在 forward 层被忽略，
    不会形成自触发循环。
    """
    cfg = store.load()
    if str(getattr(chat, "who", "") or "") == cfg.get("admin_group"):
        if _try_batch(bot, chat, msg, cfg):
            return True
        return handle_admin_message(bot, chat, msg, cfg)
    return False


def _try_batch(bot, chat, msg, cfg) -> bool:
    """管理群里的 Phase3 批量纳管指令（批量备注/预览/回写Notion）。命中返回 True。"""
    if str(getattr(msg, "type", "") or "") != "text":
        return False
    content = str(getattr(msg, "content", "") or "").strip()
    from .common import is_bot_reply
    if not content or is_bot_reply(content):
        return False
    try:
        from . import batch
        return batch.handle_batch_command(bot, chat, cfg, content)
    except Exception as e:
        from .common import log
        log("ERROR", f"批量指令出错: {e}")
        return False


def handle_system_message(bot, chat, msg) -> bool:
    """system 消息入口：识别新人进群，发送分群迎新。"""
    cfg = store.load()
    return handle_welcome(bot, chat, msg, cfg)
