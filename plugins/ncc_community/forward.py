# -*- coding: utf-8 -*-
"""管理群转发与管理指令 —— 引擎的②（菜单式转发）。

核心交互（都在管理群内，群成员即管理员）：

    转发            -> 机器人回带编号的分组菜单
    3               -> 选第 3 个分组，进入收集模式
    <任意消息若干条>  -> 每条实时 msg.forward() 到该分组所有【允许转发】的群
    结束             -> 退出并汇报

分组与权限来自 Notion（同步到本地 registry），不在群里维护——发「同步」即时拉取。
收集会话按发送人隔离，多位管理员可同时各转各的。
机器人自己的回复带 REPLY_PREFIX，指令层忽略，避免自触发。
"""
from __future__ import annotations

import re
import time
import threading

from . import store, registry
from .common import REPLY_PREFIX, is_bot_reply, log, reply

# 收集会话：sender -> {"grouping", "targets", "last_active", "ok", "fail"}
_SESSIONS = {}
# 待选菜单：sender -> {"menu": [分组名...], "ts": t}
_PENDING_MENU = {}
_STATE_LOCK = threading.Lock()

# 主窗口操作锁（打备注/检查群组会切主窗口，避免并发互踩）
MAIN_WINDOW_LOCK = threading.Lock()

MENU_TTL = 120  # 菜单待选有效期（秒）

HELP_TEXT = """肥肉管理指令（本群成员即管理员）：
【转发】
  转发            列出分组菜单，回复编号即可开始
  转发 <分组名>    直接开始转发到该分组
  结束 / 取消      结束本次转发
【查看】
  分组列表         查看所有转发分组及群数
  待归类           查看新发现、待你在 Notion 归类的群
  同步             立刻从 Notion 拉取最新分组/权限
  检查群组 <分组>   逐群试连通并汇报
【拉群关键词】
  拉群列表 / 设拉群 <关键词>|<目标群> / 删拉群 <关键词>
【迎新】
  迎新列表 / 开迎新 <群名> / 关迎新 <群名>
  设迎新文案 <群名>|<文案>（{name}=新人昵称）/ 设迎新链接 <群名>|<链接>
说明：分组和「允许转发/发言」在 Notion『群聊列表』里维护，改完发「同步」。
带 | 的指令用竖线分隔参数（群名里常有空格）。"""

_NON_FORWARDABLE_TYPES = {"time", "other"}


def handle_admin_message(bot, chat, msg, cfg) -> bool:
    """管理群消息入口。返回 True 表示已处理。"""
    attr = str(getattr(msg, "attr", "") or "")
    if attr not in ("friend", "self"):
        return False

    sender = str(getattr(msg, "sender", "") or "")
    content = str(getattr(msg, "content", "") or "")
    mtype = str(getattr(msg, "type", "") or "")

    if is_bot_reply(content):
        return False

    _expire_sessions(chat, cfg)

    if mtype == "text":
        text = content.strip()
        # 优先：待选菜单下回复纯数字 = 选分组
        if _resolve_menu_selection(chat, cfg, sender, text):
            return True
        if _try_command(bot, chat, cfg, sender, text):
            return True

    session = _get_session(sender)
    if session is not None:
        if mtype in _NON_FORWARDABLE_TYPES:
            return False
        return _forward_material(chat, msg, cfg, sender, session)

    return False


# ------------------------------------------------------------------ 指令解析

def _try_command(bot, chat, cfg, sender, text) -> bool:
    if not text:
        return False
    plain = text.replace(" ", "")

    if plain in ("帮助", "肥肉帮助", "ncc", "NCC", "指令"):
        reply(chat, HELP_TEXT); return True
    if plain in ("转发", "转发菜单"):
        return _show_menu(chat, sender)
    if plain in ("分组列表", "转发分组"):
        reply(chat, _format_groupings()); return True
    if plain in ("待归类", "新群", "待归类群"):
        reply(chat, _format_pending()); return True
    if plain in ("同步", "拉取", "拉取notion", "拉取Notion", "刷新"):
        return _do_sync(chat)
    if plain in ("结束", "完成"):
        return _end_session(chat, sender, cancelled=False)
    if plain == "取消":
        return _end_session(chat, sender, cancelled=True)
    if plain == "拉群列表":
        reply(chat, _format_invites(cfg)); return True
    if plain == "迎新列表":
        reply(chat, _format_welcomes(cfg)); return True

    m = re.match(r"^(转发|检查群组|开迎新|关迎新|删拉群)\s*(.+)$", text, re.S)
    if m:
        name = m.group(2).strip()
        return {
            "转发": lambda: _start_session(chat, sender, name),
            "检查群组": lambda: _check_groups(bot, chat, name),
            "开迎新": lambda: _toggle_welcome(chat, cfg, name, True),
            "关迎新": lambda: _toggle_welcome(chat, cfg, name, False),
            "删拉群": lambda: _delete_invite(chat, cfg, name),
        }[m.group(1)]()

    m = re.match(r"^(设拉群|设迎新文案|设迎新链接)\s*(.+?)\s*\|\s*(.*)$", text, re.S)
    if m:
        cmd, a, b = m.group(1), m.group(2).strip(), m.group(3).strip()
        return {
            "设拉群": lambda: _set_invite(chat, cfg, a, b),
            "设迎新文案": lambda: _set_welcome_field(chat, cfg, a, "text", b),
            "设迎新链接": lambda: _set_welcome_field(chat, cfg, a, "url", b),
        }[cmd]()

    return False


# ------------------------------------------------------------------ 菜单

def _show_menu(chat, sender) -> bool:
    data = registry.load()
    groupings = registry.list_forward_groupings(data)
    if not groupings:
        reply(chat, "还没有可转发的分组。先发「同步」从 Notion 拉取，"
                    "并确认 Notion 里分组勾了「是否转发」、群勾了「允许转发」。")
        return True
    with _STATE_LOCK:
        _PENDING_MENU[sender] = {"menu": [g[0] for g in groupings], "ts": time.time()}
    lines = ["选择要转发到的分组（回复编号）："]
    for i, (name, cnt) in enumerate(groupings, 1):
        lines.append(f"  {i}. {name}（{cnt} 群）")
    lines.append("——回复数字开始，或发「取消」放弃。")
    reply(chat, "\n".join(lines))
    return True


def _resolve_menu_selection(chat, cfg, sender, text) -> bool:
    """待选菜单存在且用户回复纯数字时，解析为分组并开始收集。"""
    if not re.fullmatch(r"\d{1,2}", text):
        return False
    with _STATE_LOCK:
        pend = _PENDING_MENU.get(sender)
        if not pend or time.time() - pend["ts"] > MENU_TTL:
            _PENDING_MENU.pop(sender, None)
            return False
        menu = pend["menu"]
    idx = int(text)
    if idx < 1 or idx > len(menu):
        reply(chat, f"编号超范围，请回 1~{len(menu)}。")
        return True
    grouping = menu[idx - 1]
    with _STATE_LOCK:
        _PENDING_MENU.pop(sender, None)
    return _start_session(chat, sender, grouping)


# ------------------------------------------------------------------ 收集会话

def _get_session(sender):
    with _STATE_LOCK:
        return _SESSIONS.get(sender)


def _start_session(chat, sender, grouping) -> bool:
    data = registry.load()
    if grouping not in data.get("groupings", {}):
        reply(chat, f"没有「{grouping}」这个分组。发「分组列表」看看，或「同步」拉取。")
        return True
    targets = registry.targets_for_grouping(data, grouping)
    if not targets:
        reply(chat, f"分组「{grouping}」下没有【允许转发】的群。"
                    f"去 Notion 给群勾上「允许转发」再发「同步」。")
        return True
    with _STATE_LOCK:
        _SESSIONS[sender] = {"grouping": grouping, "targets": list(targets),
                             "last_active": time.time(), "ok": 0, "fail": 0}
    reply(chat, f"开始转发到「{grouping}」（{len(targets)} 个群）。\n"
                f"现在发的每条消息（文字/图片/视频/文件/链接均可）都会群发；"
                f"发「结束」收工。")
    return True


def _end_session(chat, sender, cancelled) -> bool:
    with _STATE_LOCK:
        session = _SESSIONS.pop(sender, None)
        _PENDING_MENU.pop(sender, None)
    if session is None:
        if cancelled:
            reply(chat, "已取消。")
        else:
            reply(chat, "当前没有进行中的转发。发「转发」选分组开始。")
        return True
    if cancelled:
        reply(chat, "已取消转发模式。")
    else:
        reply(chat, f"转发结束 ✅ 分组「{session['grouping']}」："
                    f"成功 {session['ok']} 条次，失败 {session['fail']} 条次。")
    return True


def _expire_sessions(chat, cfg):
    timeout = int(cfg.get("forward", {}).get("session_timeout", 300))
    now = time.time()
    expired = []
    with _STATE_LOCK:
        for sender, session in list(_SESSIONS.items()):
            if now - session["last_active"] > timeout:
                expired.append((sender, session))
                del _SESSIONS[sender]
        for sender in [s for s, p in _PENDING_MENU.items() if now - p["ts"] > MENU_TTL]:
            _PENDING_MENU.pop(sender, None)
    for sender, session in expired:
        reply(chat, f"@{sender} 的转发会话闲置超时，已自动结束"
                    f"（成功 {session['ok']}，失败 {session['fail']}）。")


def _forward_material(chat, msg, cfg, sender, session) -> bool:
    targets = session["targets"]
    chunk_size = max(1, int(cfg.get("forward", {}).get("chunk_size", 8)))
    ok_list, fail_list = [], []
    for i in range(0, len(targets), chunk_size):
        part = targets[i:i + chunk_size]
        try:
            result = msg.forward(part)
            # wxautox4 实测：成功返回 None
            if result is None:
                success, err = True, ""
            else:
                success = bool(result)
                err = "" if success else _wxresponse_message(result)
        except Exception as e:
            success, err = False, str(e)
        if success:
            ok_list.extend(part)
        else:
            fail_list.extend(part)
            log("ERROR", f"转发到 {part} 失败: {err}")
        time.sleep(1)
    session["ok"] += len(ok_list)
    session["fail"] += len(fail_list)
    session["last_active"] = time.time()
    if fail_list:
        reply(chat, f"该条已转发 {len(ok_list)}/{len(targets)} 个群，失败：{'、'.join(fail_list)}")
    else:
        reply(chat, f"已转发到 {len(ok_list)} 个群 ✅")
    return True


def _wxresponse_message(result) -> str:
    try:
        return str(result["message"])
    except Exception:
        return str(result)


# ------------------------------------------------------------------ 查看 / 同步

def _format_groupings() -> str:
    data = registry.load()
    groupings = registry.list_forward_groupings(data)
    if not groupings:
        synced = data.get("synced_at")
        tip = "发「同步」从 Notion 拉取。" if not synced else "Notion 里没有勾了「是否转发」的分组。"
        return "还没有可转发的分组。" + tip
    lines = [f"转发分组（共 {len(groupings)} 个，来自 Notion）："]
    for i, (name, cnt) in enumerate(groupings, 1):
        lines.append(f"  {i}. {name}（{cnt} 群）")
    lines.append(f"最近同步：{data.get('synced_at') or '未同步'}")
    return "\n".join(lines)


def _format_pending() -> str:
    data = registry.load()
    pend = [name for name, g in data.get("groups", {}).items() if g.get("status") == "pending"]
    if not pend:
        return "没有待归类的新群。"
    lines = [f"待归类新群（{len(pend)} 个，请去 Notion『群聊列表』选分组+勾允许转发）："]
    lines.extend(f"  - {n}" for n in pend)
    return "\n".join(lines)


def _do_sync(chat) -> bool:
    try:
        from . import notion_sync
        stat = notion_sync.pull()
        reply(chat, f"已从 Notion 同步 ✅ 分组 {stat['groupings']} 个、"
                    f"群 {stat['groups']} 个（允许转发 {stat['forward_on']} 个）。")
    except Exception as e:
        reply(chat, f"同步失败：{e}")
        log("ERROR", f"Notion 同步失败: {e}")
    return True


def _check_groups(bot, chat, name) -> bool:
    data = registry.load()
    if name in ("全部", "所有", "all"):
        targets = sorted({registry.target(g) for g in data.get("groups", {}).values()
                          if g.get("allow_forward")})
    elif name in data.get("groupings", {}):
        targets = registry.targets_for_grouping(data, name)
    else:
        reply(chat, f"没有「{name}」这个分组。发「分组列表」看看。")
        return True
    if not targets:
        reply(chat, "该分组没有允许转发的群可检查。")
        return True
    reply(chat, f"开始检查 {len(targets)} 个群的可达性，请稍等…")
    ok_list, fail_list = [], []
    wx = getattr(bot, "wx", None)
    for t in targets:
        try:
            with MAIN_WINDOW_LOCK:
                wx.ChatWith(who=t, exact=False)
            ok_list.append(t)
        except Exception as e:
            fail_list.append(t)
            log("WARNING", f"检查群组 {t} 不可达: {e}")
        time.sleep(0.5)
    lines = [f"检查完成：{len(ok_list)}/{len(targets)} 可达。"]
    if fail_list:
        lines.append("不可达（可能改了群名/退群，去 Notion 更新）：")
        lines.extend(f" - {t}" for t in fail_list)
    reply(chat, "\n".join(lines))
    return True


# ------------------------------------------------------------------ 拉群 / 迎新（仍存 config.json）

def _format_invites(cfg) -> str:
    kw = cfg.get("invite", {}).get("keywords", {})
    if not kw:
        return "还没有拉群关键词。用「设拉群 <关键词>|<目标群>」添加。"
    lines = ["拉群关键词："]
    lines.extend(f"◾ {k} → {v}" for k, v in kw.items())
    lines.append("（用户私聊我或在群里发关键词即可被拉群）")
    return "\n".join(lines)


def _set_invite(chat, cfg, keyword, target) -> bool:
    if not keyword or not target:
        reply(chat, "格式：设拉群 <关键词>|<目标群>"); return True
    cfg.setdefault("invite", {}).setdefault("keywords", {})[keyword] = target
    store.save(cfg)
    reply(chat, f"已设置：发「{keyword}」→ 拉进「{target}」。"); return True


def _delete_invite(chat, cfg, keyword) -> bool:
    kw = cfg.setdefault("invite", {}).setdefault("keywords", {})
    if keyword not in kw:
        reply(chat, f"没有「{keyword}」这个拉群关键词。"); return True
    del kw[keyword]; store.save(cfg)
    reply(chat, f"拉群关键词「{keyword}」已删除。"); return True


def _format_welcomes(cfg) -> str:
    ws = cfg.get("welcome", {})
    if not ws:
        return "还没有迎新配置。用「设迎新文案 <群名>|<文案>」添加。"
    lines = ["迎新配置："]
    for group, w in ws.items():
        state = "开" if w.get("enabled") else "关"
        url = w.get("url") or "（无链接）"
        lines.append(f"◾ [{state}] {group}\n   文案：{w.get('text', '')}\n   链接：{url}")
    return "\n".join(lines)


def _toggle_welcome(chat, cfg, group, enabled) -> bool:
    ws = cfg.setdefault("welcome", {})
    w = ws.setdefault(group, {"enabled": enabled, "text": "欢迎 {name} 加入！🎉", "url": ""})
    w["enabled"] = enabled; store.save(cfg)
    reply(chat, f"群「{group}」迎新已{'开启' if enabled else '关闭'}。"); return True


def _set_welcome_field(chat, cfg, group, field, value) -> bool:
    ws = cfg.setdefault("welcome", {})
    w = ws.setdefault(group, {"enabled": True, "text": "欢迎 {name} 加入！🎉", "url": ""})
    w[field] = value; store.save(cfg)
    label = "文案" if field == "text" else "链接"
    reply(chat, f"群「{group}」迎新{label}已更新：{value or '（已清除）'}"); return True
