# -*- coding: utf-8 -*-
"""关键词拉群：私聊/群聊命中关键词后，把发送人拉进目标群。

UI 协议的边界（写给未来维护者）：
- 微信"添加群成员"选人框只能搜到机器人的好友；非好友群友拉不动，
  失败时机器人会回话引导先加好友。
- 按昵称匹配，同名好友有极小概率错拉——活动场景可接受。
- 群超过 200 人后被拉人需要点邀请确认卡片，属微信机制，非故障。
"""
from __future__ import annotations

import threading
import time
from datetime import date

from .common import REPLY_PREFIX, is_bot_reply, log
from .forward import MAIN_WINDOW_LOCK

# (person, keyword) -> (date, count)，进程内即可，重启清零无伤大雅
_QUOTA = {}
_QUOTA_LOCK = threading.Lock()


def _quota_ok(person: str, keyword: str, daily_limit: int) -> bool:
    today = date.today().isoformat()
    with _QUOTA_LOCK:
        day, count = _QUOTA.get((person, keyword), (today, 0))
        if day != today:
            count = 0
        if count >= daily_limit:
            return False
        _QUOTA[(person, keyword)] = (today, count + 1)
        return True


def handle_invite(bot, chat, msg, cfg) -> bool:
    """好友消息入口（非管理群）。命中拉群关键词返回 True。"""
    icfg = cfg.get("invite") or {}
    keywords = icfg.get("keywords") or {}
    if not keywords:
        return False
    if str(getattr(msg, "type", "") or "") != "text":
        return False

    content = str(getattr(msg, "content", "") or "").strip()
    if is_bot_reply(content):
        return False
    # 群里 "@肥肉 关键词" 也能触发
    at_me = str(getattr(getattr(bot, "config", None), "AtMe", "") or "")
    if at_me:
        content = content.replace(at_me, "").strip()

    target = keywords.get(content)
    if not target:
        return False

    chat_type = str(getattr(chat, "chat_type", "") or "")
    is_group = chat_type == "group"
    person = (str(getattr(msg, "sender", "") or "") if is_group
              else str(getattr(chat, "who", "") or ""))
    if not person:
        return False
    me = str(getattr(getattr(bot, "wx", None), "nickname", "") or "")
    if me and person == me:
        return False

    if not _quota_ok(person, content, int(icfg.get("daily_limit", 3))):
        _safe_send(chat, f"{REPLY_PREFIX} 今天的拉群次数用完啦，明天再来~")
        return True

    ok, err = _invite(bot, target, person)
    if ok:
        _safe_send(chat, f"{REPLY_PREFIX} 已邀请你加入「{target}」，"
                         f"如群人数较多请留意确认邀请卡片哦~")
        log("INFO", f"拉群成功：{person} → {target}（关键词「{content}」）")
    else:
        _safe_send(chat, f"{REPLY_PREFIX} 拉群没成功（{err}）。"
                         f"可能咱们还不是好友，先加我好友再试；或者喊管理员手动拉你~")
        log("WARNING", f"拉群失败：{person} → {target}：{err}")
    return True


def _invite(bot, group: str, person: str):
    """切主窗口到目标群并添加成员。返回 (成功?, 错误信息)。"""
    wx = getattr(bot, "wx", None)
    if wx is None:
        return False, "机器人未就绪"
    with MAIN_WINDOW_LOCK:
        try:
            wx.ChatWith(who=group, exact=True)
            time.sleep(0.5)
            result = wx.AddGroupMembers(members=[person])
            if result:
                return True, ""
            return False, _wxresponse_message(result)
        except Exception as e:
            return False, str(e)


def _wxresponse_message(result) -> str:
    try:
        return str(result["message"])
    except Exception:
        return str(result)


def _safe_send(chat, text: str) -> None:
    try:
        chat.SendMsg(msg=text)
    except Exception as e:
        log("ERROR", f"拉群回复发送失败: {e}")
