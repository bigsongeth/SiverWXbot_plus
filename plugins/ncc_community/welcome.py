# -*- coding: utf-8 -*-
"""分群迎新：解析"xxx 加入了群聊"系统消息，发送定制欢迎语 + 链接卡片。

每个群可配置独立的文案与在地化链接（黄山/大理/三亚/线上各不相同），
配置见 store 的 welcome 段，管理群内可用 设迎新文案/设迎新链接/开迎新 维护。
"""
from __future__ import annotations

import re
import time

from .common import log

# 微信系统消息里的名字可能被直引号或弯引号包裹，两种都兼容。
_QUOTE = r'[“”"]'
_PATTERNS = [
    # 「你邀请"张三"加入了群聊」「"李四"邀请"张三"加入了群聊」——取被邀请人
    re.compile(_QUOTE + r'(?P<name>[^“”"]+)' + _QUOTE + r'加入了群聊'),
    # 「"张三"通过扫描…二维码加入群聊」
    re.compile(_QUOTE + r'(?P<name>[^“”"]+)' + _QUOTE + r'通过扫描.{0,20}二维码加入群聊'),
]


def parse_new_member(content: str):
    """从系统消息文本里解析新人昵称，解析不出返回 None。"""
    content = str(content or "")
    if "加入" not in content:
        return None
    for pattern in _PATTERNS:
        matches = list(pattern.finditer(content))
        if matches:
            # 邀请句式里最后一个引号名是新人（前面的是邀请人）
            return matches[-1].group("name")
    return None


def handle_welcome(bot, chat, msg, cfg) -> bool:
    """系统消息入口。命中迎新配置的群且解析出新人时发送欢迎，返回 True。"""
    who = str(getattr(chat, "who", "") or "")
    wcfg = (cfg.get("welcome") or {}).get(who)
    if not wcfg or not wcfg.get("enabled"):
        return False

    name = parse_new_member(getattr(msg, "content", ""))
    if not name:
        return False

    # 新人是机器人自己（被拉进新群）时不自我欢迎
    me = str(getattr(getattr(bot, "wx", None), "nickname", "") or "")
    if me and name == me:
        return False

    text = str(wcfg.get("text") or "欢迎 {name} 加入！🎉").replace("{name}", name)
    try:
        chat.SendMsg(msg=text)
    except Exception as e:
        log("ERROR", f"迎新文案发送失败（群 {who}）: {e}")
        return False

    url = str(wcfg.get("url") or "").strip()
    if url:
        try:
            time.sleep(1.5)
            # SendUrlCard 走主窗口按名称投递，发出的是链接卡片
            bot.wx.SendUrlCard(url=url, friends=who)
        except Exception as e:
            log("ERROR", f"迎新链接卡片发送失败（群 {who}）: {e}")

    log("INFO", f"已迎新：{who} 的新成员 {name}")
    return True
