# -*- coding: utf-8 -*-
"""关键词拉群：私聊命中关键词后，把发送人拉进目标群（群聊不触发，避免闲聊误拉）。

UI 协议的边界（写给未来维护者）：
- 微信"添加群成员"选人框只能搜到机器人的好友；非好友群友拉不动，
  失败时机器人会回话引导先加好友。
- 按昵称匹配，同名好友有极小概率错拉——活动场景可接受。
- 群超过 200 人后被拉人需要点邀请确认卡片，属微信机制，非故障。

2026-07-30 事故复盘（07-29 22:02、07-30 01:55 连挂两次，两次都报"未选择任何新增成员"）：
真正断的是【切群】那一步，不是选人。
- wxautox 的 ChatWith 在会话列表里找不到就走搜索框，只等 WxParam.SEARCH_CHAT_TIMEOUT
  秒（40.1.15 默认 2 秒，现已在 wxbot_core.py 提到 5），超时静默返回 failure("未找到会话")。
- 旧代码不接返回值，切群失败后照样调 AddGroupMembers：于是在【残留的私聊窗口】上点
  "聊天信息 → 添加成员"，选人框里当然选不到人，报出误导人的"未选择任何新增成员"；
  更危险的是万一真选中了谁，微信会直接【新建一个群】。
本文件的对策：
1) 切群必须验证成功——既接 ChatWith 返回值，又用 ChatInfo 复核当前窗口就是目标群
   （chat_type == group 且名字对得上），否则早退并按"群没找到"报错；
2) 备注名搜不到时回退用群名再试（🐶备注丢了/没生效时不至于全废）；
3) 搜索慢就多试几次，别一次超时就判死；
4) 失败不吃当天配额，并给管理群留一条提醒（昨晚那位问"管理员在哪啊"没人接得住）。
"""
from __future__ import annotations

import threading
import time
from datetime import date

from . import registry
from .common import REPLY_PREFIX, is_bot_reply, log, notify_admin
from .forward import MAIN_WINDOW_LOCK

# (person, keyword) -> (date, count)，进程内即可，重启清零无伤大雅
_QUOTA = {}
_QUOTA_LOCK = threading.Lock()

# (person, keyword) -> (date, 失败次数)。失败退配额，但只退前 _MAX_REFUNDS 次：
# 否则有人狂发关键词就能无限触发切群+选人的 UI 操作（会占住主窗口锁）并刷爆管理群。
_FAILS = {}
_MAX_REFUNDS = 3

# 切群重试：微信搜索框的结果常常不是第一时间出来，一次超时不代表这个群没了
_SWITCH_ATTEMPTS = 3
_SWITCH_WAIT = 1.5
# 切到群之后等群资料加载好，再去点"添加成员"
_SETTLE_AFTER_SWITCH = 0.8


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


def _record_failure(person: str, keyword: str) -> tuple:
    """记一次失败，并决定要不要退配额 / 要不要提醒管理群。
    返回 (今日第几次失败, 是否已退配额)。

    退配额：拉群失败不该吃掉当天额度（默认一天 3 次，全喂给失败就没得试了），
    但只退前 _MAX_REFUNDS 次，免得刷关键词能无限触发 UI 操作。
    管理群提醒只在当天第一次失败时发，同一个人反复试不刷屏。"""
    today = date.today().isoformat()
    with _QUOTA_LOCK:
        day, fails = _FAILS.get((person, keyword), (today, 0))
        if day != today:
            fails = 0
        fails += 1
        _FAILS[(person, keyword)] = (today, fails)

        refunded = False
        if fails <= _MAX_REFUNDS:
            entry = _QUOTA.get((person, keyword))
            if entry:
                qday, count = entry
                if count > 0:
                    _QUOTA[(person, keyword)] = (qday, count - 1)
                    refunded = True
    return fails, refunded


def handle_invite(bot, chat, msg, cfg) -> bool:
    """私聊好友消息入口。命中拉群关键词返回 True；群聊一律不处理。

    关键词两个来源：Notion「迎新拉群」表（发「同步」进 registry，真相源）
    + config.json 的 invite.keywords（管理群「设拉群」手工加的，同名时覆盖前者）。"""
    if str(getattr(chat, "chat_type", "") or "") == "group":
        return False
    if str(getattr(msg, "type", "") or "") != "text":
        return False

    content = str(getattr(msg, "content", "") or "").strip()
    if not content or is_bot_reply(content):
        return False

    icfg = cfg.get("invite") or {}
    reg_data = registry.load()
    keywords = dict(reg_data.get("invite_keywords") or {})
    keywords.update(icfg.get("keywords") or {})
    target = keywords.get(content)
    if not target:
        return False

    person = str(getattr(chat, "who", "") or "")
    if not person:
        return False
    me = str(getattr(getattr(bot, "wx", None), "nickname", "") or "")
    if me and person == me:
        return False

    if not _quota_ok(person, content, int(icfg.get("daily_limit", 3))):
        _safe_send(chat, f"{REPLY_PREFIX} 今天的拉群次数用完啦，明天再来~")
        return True

    addresses = _candidate_addresses(reg_data, target)
    kind, detail = _invite(bot, addresses, person)
    if kind == "ok":
        # 成功时静默拉群，不回话（用户要求：拉群就好，别多发消息）
        log("INFO", f"拉群成功：{person} → {target}（关键词「{content}」，寻址「{detail}」）")
        return True

    # 失败：额度退回、按原因分流回话、给管理群留痕
    fails, refunded = _record_failure(person, content)
    log("WARNING", f"拉群失败[{kind}]：{person} → {target}"
                   f"（关键词「{content}」，寻址候选 {addresses}，今日第 {fails} 次失败"
                   f"{'，已退配额' if refunded else ''}）：{detail}")
    if kind == "group":
        _safe_send(chat, f"{REPLY_PREFIX} 这个群我这会儿没打开成功，已经喊管理员来处理啦，"
                         f"稍等一下~")
        reason = f"切到群「{target}」失败（{detail}）——群可能改了名/解散/机器人被踢，去核对一下"
    elif kind == "member":
        _safe_send(chat, f"{REPLY_PREFIX} 拉群没成功（{detail}）。"
                         f"可能咱们还不是好友，先加我好友再试；或者喊管理员手动拉你~")
        reason = f"在「{target}」的选人框里没选到「{person}」（{detail}）——多为还不是好友"
    else:
        _safe_send(chat, f"{REPLY_PREFIX} 拉群出了点岔子，已经喊管理员来处理啦，稍等一下~")
        reason = f"拉「{person}」进「{target}」时报错：{detail}"
    if fails == 1:   # 同一人同一关键词当天只提醒一次，别刷管理群
        notify_admin(bot, cfg,
                     f"拉群没成功：「{person}」想进「{target}」（关键词「{content}」）。\n"
                     f"{reason}。\n麻烦手动拉一下。")
    return True


def _candidate_addresses(reg_data, target: str) -> list:
    """目标群的寻址串候选，按优先级排：登记表寻址（打过🐶备注就用备注）→ 群名 → 备注 → 原样。

    必须有回退：2026-07-30 那两次失败就疑似备注名在搜索框里搜不到（备注丢了或没真生效），
    只试一个串就等于整个关键词永久报废。"""
    cands = []
    g = registry.get_group(reg_data, target)
    if g:
        cands += [registry.target(g), g.get("name") or "", g.get("remark") or ""]
    cands.append(target)
    out = []
    for c in cands:
        c = str(c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _invite(bot, addresses, person):
    """切到目标群并添加成员。返回 (kind, detail)：
    - "ok"     detail = 实际生效的寻址串
    - "group"  切群失败（群改名/解散/被踢/搜索超时），detail = 原因
    - "member" 选人框里没选到人（多为非好友），detail = wxautox 的原话
    - "error"  异常，detail = 异常文本
    """
    wx = getattr(bot, "wx", None)
    if wx is None:
        return "error", "机器人未就绪"
    with MAIN_WINDOW_LOCK:
        used = ""
        last_err = "未找到会话"
        for address in addresses:
            ok, err = _switch_to_group(wx, address, addresses)
            if ok:
                used = address
                break
            last_err = err
            log("WARNING", f"切到群「{address}」失败：{err}")
        if not used:
            return "group", last_err

        time.sleep(_SETTLE_AFTER_SWITCH)
        try:
            result = wx.AddGroupMembers(members=[person])
        except Exception as e:
            return "error", str(e)
        # wxautox 有成功返回 None 的先例（msg.forward 就是），宽松判成功
        if result is None or result:
            return "ok", used
        return "member", _wxresponse_message(result)


def _switch_to_group(wx, address: str, known_names) -> tuple:
    """把主窗口切到 address 指的群，并确认真的切过去了。返回 (是否成功, 失败原因)。

    ChatWith 找不到会话是【静默失败】（返回 falsy WxResponse，不抛异常），所以既要接返回值，
    又要用 ChatInfo 复核——两道都过才算切成功，杜绝在私聊窗口上点"添加成员"（会新建群）。
    复核优先于返回值：万一某个 wxautox 版本成功时也返回 falsy，只要窗口对了就照样放行。"""
    last = ""
    for attempt in range(_SWITCH_ATTEMPTS):
        if attempt:
            time.sleep(_SWITCH_WAIT)   # 给微信搜索结果渲染的时间
        try:
            r = wx.ChatWith(who=address, exact=True)
        except Exception as e:
            last = str(e)
            continue
        switch_err = "" if (r is None or r) else (_wxresponse_message(r) or "未找到会话")
        state, why = _inspect_current_chat(wx, address, known_names)
        if state == "match":
            return True, ""
        if state == "mismatch":
            last = why
            continue
        # state == "unknown"：读不到窗口信息，只能信 ChatWith 的返回值
        if not switch_err:
            return True, ""
        last = switch_err
    return False, last or "未找到会话"


def _inspect_current_chat(wx, address: str, known_names) -> tuple:
    """复核主窗口当前停在哪。返回 ("match"|"mismatch"|"unknown", 说明)。

    读不到窗口信息时返回 unknown 交给调用方决策——复核只用来拦错，不该因为复核本身
    失败就拒绝拉群。"""
    try:
        info = wx.ChatInfo() or {}
    except Exception as e:
        log("WARNING", f"复核当前聊天窗口失败（按未知处理）：{e}")
        return "unknown", ""
    if not isinstance(info, dict) or not info:
        return "unknown", ""

    chat_type = str(info.get("chat_type") or "")
    name = str(info.get("chat_name") or "").strip()
    remark = str(info.get("remark") or "").strip()
    if chat_type and chat_type != "group":
        return "mismatch", f"切过去的不是群聊（chat_type={chat_type}，当前「{name or '未知'}」）"
    if not (name or remark):
        return "unknown", ""
    accept = {str(x).strip() for x in list(known_names) + [address] if str(x or "").strip()}
    if {name, remark} & accept:
        return "match", ""
    return "mismatch", f"切过去的是「{name or remark}」，不是目标群"


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
