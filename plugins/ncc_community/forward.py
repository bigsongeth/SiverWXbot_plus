# -*- coding: utf-8 -*-
"""管理群转发与管理指令。

核心交互（都发生在管理群内，群成员即管理员）：

    转发 测试组      -> 进入收集模式
    <任意消息若干条>  -> 每条实时 msg.forward() 到分组内所有群
    结束             -> 退出收集模式并汇报

收集会话按"发送人"隔离，多位管理员可同时各转各的。
机器人自己的回复统一带 REPLY_PREFIX，指令层直接忽略，避免自触发。
"""
from __future__ import annotations

import re
import time
import threading

from . import store
from .common import REPLY_PREFIX, is_bot_reply, log, reply

# 收集会话：sender -> {"group_name", "targets", "last_active", "ok", "fail"}
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()

# 主窗口操作锁（检查群组等会切主窗口聊天，避免并发互踩）
MAIN_WINDOW_LOCK = threading.Lock()

HELP_TEXT = """肥肉管理指令（本群成员即管理员）：
【转发】
  转发 <分组>      开始转发，之后你发的每条消息都会群发到该分组
  结束             结束本次转发
  取消             同上（不发汇报统计）
  分组列表         查看所有转发分组
【分组维护】
  新建分组 <分组>
  删除分组 <分组>
  加群 <分组>|<群名>
  删群 <分组>|<群名>
  检查群组 <分组>   逐群试连通并汇报
【拉群关键词】
  拉群列表
  设拉群 <关键词>|<目标群>
  删拉群 <关键词>
【迎新】
  迎新列表
  开迎新 <群名> / 关迎新 <群名>
  设迎新文案 <群名>|<文案>（{name} 会替换成新人昵称）
  设迎新链接 <群名>|<链接>（留空清除）
带 | 的指令用竖线分隔参数（群名里常有空格）。"""

# 不参与"收集转发"的消息类型（时间条、系统提示等）
_NON_FORWARDABLE_TYPES = {"time", "other"}


def handle_admin_message(bot, chat, msg, cfg) -> bool:
    """管理群消息入口。返回 True 表示已处理（核心跳过 AI 等后续流程）。"""
    attr = str(getattr(msg, "attr", "") or "")
    if attr not in ("friend", "self"):
        return False

    sender = str(getattr(msg, "sender", "") or "")
    content = str(getattr(msg, "content", "") or "")
    mtype = str(getattr(msg, "type", "") or "")

    if is_bot_reply(content):
        return False  # 机器人自己的回复

    _expire_sessions(chat, cfg)

    if mtype == "text":
        handled = _try_command(bot, chat, cfg, sender, content.strip())
        if handled:
            return True

    session = _get_session(sender)
    if session is not None:
        if mtype in _NON_FORWARDABLE_TYPES:
            return False
        return _forward_material(chat, msg, cfg, sender, session)

    return False


# ------------------------------------------------------------------
# 指令解析
# ------------------------------------------------------------------

def _try_command(bot, chat, cfg, sender, text) -> bool:
    """尝试把文本按指令执行。命中返回 True。"""
    if not text:
        return False

    # ---- 无参数指令 ----
    plain = text.replace(" ", "")
    if plain in ("帮助", "肥肉帮助", "ncc", "NCC", "指令"):
        reply(chat, HELP_TEXT)
        return True
    if plain in ("分组列表", "转发分组"):
        reply(chat, _format_groups(cfg))
        return True
    if plain in ("结束", "完成"):
        return _end_session(chat, sender, cancelled=False)
    if plain == "取消":
        return _end_session(chat, sender, cancelled=True)
    if plain == "拉群列表":
        reply(chat, _format_invites(cfg))
        return True
    if plain == "迎新列表":
        reply(chat, _format_welcomes(cfg))
        return True

    # ---- 带参数指令 ----
    m = re.match(r"^(转发|新建分组|删除分组|检查群组|开迎新|关迎新|删拉群)\s*(.+)$", text, re.S)
    if m:
        name = m.group(2).strip()
        return {
            "转发": lambda: _start_session(chat, cfg, sender, name),
            "新建分组": lambda: _create_group(chat, cfg, name),
            "删除分组": lambda: _delete_group(chat, cfg, name),
            "检查群组": lambda: _check_groups(bot, chat, cfg, name),
            "开迎新": lambda: _toggle_welcome(chat, cfg, name, True),
            "关迎新": lambda: _toggle_welcome(chat, cfg, name, False),
            "删拉群": lambda: _delete_invite(chat, cfg, name),
        }[m.group(1)]()

    m = re.match(r"^(加群|删群|设拉群|设迎新文案|设迎新链接)\s*(.+?)\s*\|\s*(.*)$", text, re.S)
    if m:
        cmd, a, b = m.group(1), m.group(2).strip(), m.group(3).strip()
        return {
            "加群": lambda: _add_group_target(chat, cfg, a, b),
            "删群": lambda: _remove_group_target(chat, cfg, a, b),
            "设拉群": lambda: _set_invite(chat, cfg, a, b),
            "设迎新文案": lambda: _set_welcome_field(chat, cfg, a, "text", b),
            "设迎新链接": lambda: _set_welcome_field(chat, cfg, a, "url", b),
        }[cmd]()

    return False


# ------------------------------------------------------------------
# 收集会话
# ------------------------------------------------------------------

def _get_session(sender):
    with _SESSIONS_LOCK:
        return _SESSIONS.get(sender)


def _start_session(chat, cfg, sender, group_name) -> bool:
    groups = cfg.get("forward", {}).get("groups", {})
    targets = groups.get(group_name)
    if targets is None:
        reply(chat, f"没有「{group_name}」这个分组。发「分组列表」看看现有分组。")
        return True
    if not targets:
        reply(chat, f"分组「{group_name}」还没有群，先用「加群 {group_name}|<群名>」添加。")
        return True
    with _SESSIONS_LOCK:
        _SESSIONS[sender] = {
            "group_name": group_name,
            "targets": list(targets),
            "last_active": time.time(),
            "ok": 0,
            "fail": 0,
        }
    timeout_min = int(cfg.get("forward", {}).get("session_timeout", 300)) // 60
    reply(chat,
          f"开始转发到「{group_name}」（{len(targets)} 个群）。\n"
          f"现在发的每条消息（文字/图片/视频/文件/链接均可）都会被群发；"
          f"发「结束」收工，{timeout_min} 分钟没动静会自动结束。")
    return True


def _end_session(chat, sender, cancelled) -> bool:
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(sender, None)
    if session is None:
        reply(chat, "当前没有进行中的转发。发「转发 <分组>」开始。")
        return True
    if cancelled:
        reply(chat, "已取消转发模式。")
    else:
        reply(chat,
              f"转发结束 ✅ 分组「{session['group_name']}」："
              f"成功 {session['ok']} 条次，失败 {session['fail']} 条次。")
    return True


def _expire_sessions(chat, cfg):
    timeout = int(cfg.get("forward", {}).get("session_timeout", 300))
    now = time.time()
    expired = []
    with _SESSIONS_LOCK:
        for sender, session in list(_SESSIONS.items()):
            if now - session["last_active"] > timeout:
                expired.append((sender, session))
                del _SESSIONS[sender]
    for sender, session in expired:
        reply(chat, f"@{sender} 的转发会话闲置超时，已自动结束"
                    f"（成功 {session['ok']}，失败 {session['fail']}）。")


def _forward_material(chat, msg, cfg, sender, session) -> bool:
    """把一条素材消息转发到会话分组的所有目标群。"""
    targets = session["targets"]
    chunk_size = max(1, int(cfg.get("forward", {}).get("chunk_size", 8)))
    ok_list, fail_list = [], []

    for i in range(0, len(targets), chunk_size):
        part = targets[i:i + chunk_size]
        try:
            result = msg.forward(part)
            # wxautox4 40.1.15 实测：成功时返回 None（与文档的 WxResponse 不符），
            # 失败时抛异常或返回 falsy WxResponse（带 message）
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
        time.sleep(1)  # 模拟人工节奏，也给 UI 喘息

    session["ok"] += len(ok_list)
    session["fail"] += len(fail_list)
    session["last_active"] = time.time()

    if fail_list:
        reply(chat, f"该条已转发 {len(ok_list)}/{len(targets)} 个群，"
                    f"失败：{'、'.join(fail_list)}")
    else:
        reply(chat, f"已转发到 {len(ok_list)} 个群 ✅")
    return True


def _wxresponse_message(result) -> str:
    try:
        return str(result["message"])
    except Exception:
        return str(result)


# ------------------------------------------------------------------
# 分组维护
# ------------------------------------------------------------------

def _format_groups(cfg) -> str:
    groups = cfg.get("forward", {}).get("groups", {})
    if not groups:
        return "还没有任何转发分组。用「新建分组 <名>」创建。"
    lines = ["转发分组："]
    for name, targets in groups.items():
        lines.append(f"◾ {name}（{len(targets)} 群）")
        for t in targets:
            lines.append(f"   - {t}")
    return "\n".join(lines)


def _create_group(chat, cfg, name) -> bool:
    groups = cfg.setdefault("forward", {}).setdefault("groups", {})
    if name in groups:
        reply(chat, f"分组「{name}」已存在。")
        return True
    groups[name] = []
    store.save(cfg)
    reply(chat, f"分组「{name}」已创建，用「加群 {name}|<群名>」添加目标群。")
    return True


def _delete_group(chat, cfg, name) -> bool:
    groups = cfg.setdefault("forward", {}).setdefault("groups", {})
    if name not in groups:
        reply(chat, f"没有「{name}」这个分组。")
        return True
    del groups[name]
    store.save(cfg)
    reply(chat, f"分组「{name}」已删除。")
    return True


def _add_group_target(chat, cfg, group_name, target) -> bool:
    if not target:
        reply(chat, "群名不能为空。格式：加群 <分组>|<群名>")
        return True
    groups = cfg.setdefault("forward", {}).setdefault("groups", {})
    if group_name not in groups:
        reply(chat, f"没有「{group_name}」这个分组。先「新建分组 {group_name}」。")
        return True
    if target in groups[group_name]:
        reply(chat, f"「{target}」已在分组「{group_name}」里。")
        return True
    groups[group_name].append(target)
    store.save(cfg)
    reply(chat, f"已把「{target}」加进分组「{group_name}」（现 {len(groups[group_name])} 群）。")
    return True


def _remove_group_target(chat, cfg, group_name, target) -> bool:
    groups = cfg.setdefault("forward", {}).setdefault("groups", {})
    if group_name not in groups or target not in groups[group_name]:
        reply(chat, f"分组「{group_name}」里没有「{target}」。")
        return True
    groups[group_name].remove(target)
    store.save(cfg)
    reply(chat, f"已把「{target}」移出分组「{group_name}」。")
    return True


def _check_groups(bot, chat, cfg, name) -> bool:
    """逐群 ChatWith 试连通。best-effort：异常视为不可达。"""
    groups = cfg.get("forward", {}).get("groups", {})
    if name in ("全部", "所有", "all"):
        targets = sorted({t for ts in groups.values() for t in ts})
    else:
        targets = groups.get(name)
        if targets is None:
            reply(chat, f"没有「{name}」这个分组。")
            return True
    if not targets:
        reply(chat, "该分组没有群可检查。")
        return True
    reply(chat, f"开始检查 {len(targets)} 个群的可达性，请稍等…")
    ok_list, fail_list = [], []
    wx = getattr(bot, "wx", None)
    for target in targets:
        try:
            with MAIN_WINDOW_LOCK:
                wx.ChatWith(who=target, exact=True)
            ok_list.append(target)
        except Exception as e:
            fail_list.append(target)
            log("WARNING", f"检查群组 {target} 不可达: {e}")
        time.sleep(0.5)
    lines = [f"检查完成：{len(ok_list)}/{len(targets)} 可达。"]
    if fail_list:
        lines.append("不可达（多半是改了群名，记得用 加群/删群 更新）：")
        lines.extend(f" - {t}" for t in fail_list)
    reply(chat, "\n".join(lines))
    return True


# ------------------------------------------------------------------
# 拉群关键词 / 迎新配置的管理指令
# ------------------------------------------------------------------

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
        reply(chat, "格式：设拉群 <关键词>|<目标群>")
        return True
    cfg.setdefault("invite", {}).setdefault("keywords", {})[keyword] = target
    store.save(cfg)
    reply(chat, f"已设置：发「{keyword}」→ 拉进「{target}」。")
    return True


def _delete_invite(chat, cfg, keyword) -> bool:
    kw = cfg.setdefault("invite", {}).setdefault("keywords", {})
    if keyword not in kw:
        reply(chat, f"没有「{keyword}」这个拉群关键词。")
        return True
    del kw[keyword]
    store.save(cfg)
    reply(chat, f"拉群关键词「{keyword}」已删除。")
    return True


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
    w["enabled"] = enabled
    store.save(cfg)
    reply(chat, f"群「{group}」迎新已{'开启' if enabled else '关闭'}。")
    return True


def _set_welcome_field(chat, cfg, group, field, value) -> bool:
    ws = cfg.setdefault("welcome", {})
    w = ws.setdefault(group, {"enabled": True, "text": "欢迎 {name} 加入！🎉", "url": ""})
    w[field] = value
    store.save(cfg)
    label = "文案" if field == "text" else "链接"
    shown = value if value else "（已清除）"
    reply(chat, f"群「{group}」迎新{label}已更新：{shown}")
    return True
