# -*- coding: utf-8 -*-
"""管理群转发与管理指令 —— 引擎的②（收集后群发，照搬旧 WCRobot 模型）。

核心交互（都在指令群内，群成员即管理员）：

    转发              -> 机器人回带编号的分组菜单
    3                 -> 选第 3 个分组，进入【收集模式】
    <消息一条条发>     -> 全部攒起来（不立即转发）
    发送              -> 后台队列开始逐群群发
    取消              -> 放弃

为什么不"来一条转一条"、不"一次多目标 forward"：
- 旧系统走协议、无 UI 限制；wxauto 是 UI 自动化，`msg.forward([多个群])` 会撞
  微信"多选转发"最多 9 个的上限。
- 所以这里改成【一群一群发】：对每个群 `msg.forward(单个群)`，彻底绕开 9 限制；
  并照搬旧系统的防风控延迟（群间 3-5s、每 10 群额外 5-10s、消息间 1-2s、重试 3 次）。
- 群发很慢（几十群×几条要几分钟），所以放【后台线程】跑，收集/接收消息不被阻塞。
- ⚠️视频号卡片 wxauto 转不了（协议能、UI 不能），发送时会识别失败并在汇报里列出。

分组与权限来自 Notion（同步到本地 registry），发「同步」即时拉取。
"""
from __future__ import annotations

import re
import time
import random
import threading
from queue import Queue

from . import store, registry
from .common import REPLY_PREFIX, is_bot_reply, log, reply

# 收集会话：sender -> {"grouping","targets","messages":[msg...],"last_active"}
_SESSIONS = {}
# 待选菜单：sender -> {"menu":[分组名...],"ts":t}
_PENDING_MENU = {}
_STATE_LOCK = threading.Lock()

# 主窗口操作锁：每次 forward 单独加锁，间隔延迟时释放，让监听线程能插空收消息
MAIN_WINDOW_LOCK = threading.Lock()

MENU_TTL = 120          # 菜单待选有效期（秒）
SESSION_TTL = 600       # 收集会话闲置超时（秒）

# 防风控延迟默认值（照搬旧 WCRobot），可被 config.forward 覆盖
DELAY = {
    "group_min": 3.0, "group_max": 5.0,       # 群间
    "batch_every": 10, "batch_min": 5.0, "batch_max": 10.0,  # 每 N 群额外歇
    "msg_min": 1.0, "msg_max": 2.0,           # 同群消息间
    "max_retries": 3,
}

# 收集时忽略的消息类型（时间条/系统提示）
_SKIP_COLLECT_TYPES = {"time", "system", "other"}

# ------------------------------------------------------------------ 后台群发队列

_QUEUE: "Queue" = Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _ensure_worker():
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            threading.Thread(target=_forward_worker, daemon=True).start()
            _WORKER_STARTED = True


def _delays(cfg):
    d = dict(DELAY)
    d.update((cfg.get("forward", {}) or {}).get("delay", {}) or {})
    return d


def _forward_one(msg, group, d) -> tuple[bool, str]:
    """把一条消息转发到单个群，带重试。返回 (成功, 错误)。"""
    last_err = ""
    for attempt in range(int(d["max_retries"])):
        try:
            with MAIN_WINDOW_LOCK:
                r = msg.forward(group)          # 单目标，绕开 9 限制
            # wxautox 成功返回 None；失败返回 falsy WxResponse 或抛异常
            if r is None or r:
                return True, ""
            last_err = _wxresponse_message(r)
        except Exception as e:
            last_err = str(e)
        time.sleep(2)
    return False, last_err


def _forward_worker():
    """后台群发线程：从队列取任务，投递并汇报。"""
    while True:
        task = _QUEUE.get()
        try:
            if task:
                _deliver(task)
        except Exception as e:
            log("ERROR", f"群发线程异常: {e}")
            try:
                _worker_report(task.get("bot"), task.get("admin"), f"{REPLY_PREFIX} 群发过程中出错：{e}")
            except Exception:
                pass
        finally:
            _QUEUE.task_done()


def _deliver(task) -> dict:
    """逐群逐条投递（可同步调用便于测试）。返回统计。带防风控延迟。"""
    bot = task["bot"]; admin = task["admin"]
    messages = task["messages"]; targets = task["targets"]
    grouping = task["grouping"]; d = task["delay"]

    ok = 0
    fail = 0
    dead_msgs = set()       # 连续失败、判定不可转发（如视频号）的消息下标
    fail_streak = {}        # 消息下标 -> 连续失败次数
    fail_detail = []

    for gi, group in enumerate(targets):
        if gi > 0 and gi % int(d["batch_every"]) == 0:
            time.sleep(random.uniform(d["batch_min"], d["batch_max"]))
        for mi, msg in enumerate(messages):
            if mi in dead_msgs:
                continue
            success, err = _forward_one(msg, group, d)
            if success:
                ok += 1
                fail_streak[mi] = 0
            else:
                fail += 1
                fail_streak[mi] = fail_streak.get(mi, 0) + 1
                mtype = str(getattr(msg, "type", "") or "")
                fail_detail.append(f"[{group}] 第{mi+1}条({mtype}): {err}")
                if fail_streak[mi] >= 2:   # 连续 2 个群都失败 → 判不可转发，后续跳过
                    dead_msgs.add(mi)
            time.sleep(random.uniform(d["msg_min"], d["msg_max"]))
        time.sleep(random.uniform(d["group_min"], d["group_max"]))

    lines = [f"{REPLY_PREFIX} 群发完成 ✅ 分组「{grouping}」",
             f"成功 {ok} 条次，失败 {fail} 条次，目标 {len(targets)} 个群 × {len(messages)} 条。"]
    if dead_msgs:
        nums = "、".join(f"第{i+1}条" for i in sorted(dead_msgs))
        lines.append(f"⚠️ {nums} 全程转发失败（视频号等 wxauto 无法转发的类型已跳过）。")
    if fail_detail and len(fail_detail) <= 12:
        lines.append("失败明细：\n" + "\n".join(fail_detail))
    elif fail_detail:
        lines.append(f"失败明细较多（{len(fail_detail)} 条），已写日志。")
        log("WARNING", "群发失败明细: " + " | ".join(fail_detail))
    _worker_report(bot, admin, "\n".join(lines))
    return {"ok": ok, "fail": fail, "dead": sorted(dead_msgs)}


def _worker_report(bot, admin, text):
    if not bot or not admin:
        return
    try:
        with MAIN_WINDOW_LOCK:
            bot.wx.SendMsg(msg=text, who=admin)
    except Exception as e:
        log("ERROR", f"群发汇报失败: {e}")


# ------------------------------------------------------------------ 消息入口

def handle_admin_message(bot, chat, msg, cfg) -> bool:
    """指令群消息入口。返回 True 表示已处理。"""
    _ensure_worker()
    attr = str(getattr(msg, "attr", "") or "")
    if attr not in ("friend", "self"):
        return False

    sender = str(getattr(msg, "sender", "") or "")
    content = str(getattr(msg, "content", "") or "")
    mtype = str(getattr(msg, "type", "") or "")

    if is_bot_reply(content):
        return False

    _expire_sessions()

    # 收集模式优先：会话存在时，除结束/取消指令外，一律当素材收集
    session = _get_session(sender)
    if session is not None:
        if mtype == "text":
            plain = content.strip().replace(" ", "")
            if plain in ("发送", "开始", "结束", "完成", "1", "go", "GO"):
                return _finish_and_enqueue(bot, chat, cfg, sender, session)
            if plain in ("取消", "0"):
                return _cancel_session(chat, sender)
        # 其它（含文字/图片/卡片）都收集
        return _collect_material(chat, sender, session, msg, mtype)

    # 非收集模式：菜单数字选择 / 指令
    if mtype == "text":
        text = content.strip()
        if _resolve_menu_selection(chat, cfg, sender, text):
            return True
        if _try_command(bot, chat, cfg, sender, text):
            return True
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
    if plain in ("拉群列表",):
        reply(chat, _format_invites(cfg)); return True
    if plain in ("迎新列表",):
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
    lines.append("——回复数字进入收集模式，或发「取消」放弃。")
    reply(chat, "\n".join(lines))
    return True


def _resolve_menu_selection(chat, cfg, sender, text) -> bool:
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
        reply(chat, f"分组「{grouping}」下没有【允许转发】的群。去 Notion 勾上「允许转发」再发「同步」。")
        return True
    with _STATE_LOCK:
        _SESSIONS[sender] = {"grouping": grouping, "targets": list(targets),
                             "messages": [], "last_active": time.time()}
    reply(chat, f"已进入转发到「{grouping}」（{len(targets)} 个群）。\n"
                f"把要群发的内容【一条条发过来】（文字/图片/公众号/链接/文件都行，"
                f"视频号可能转不了）。\n发完回复【发送】开始群发，中途【取消】退出。")
    return True


def _collect_material(chat, sender, session, msg, mtype) -> bool:
    if mtype in _SKIP_COLLECT_TYPES:
        return True  # 时间条/系统消息不收集，但算已处理（避免落到 AI）
    session["messages"].append(msg)
    session["last_active"] = time.time()
    n = len(session["messages"])
    reply(chat, f"已收集 {n} 条，继续发或回复【发送】开始群发。")
    return True


def _finish_and_enqueue(bot, chat, cfg, sender, session) -> bool:
    msgs = session["messages"]
    if not msgs:
        reply(chat, "还没收集到内容呢，先把要转发的消息发过来。")
        return True
    targets = session["targets"]
    grouping = session["grouping"]
    with _STATE_LOCK:
        _SESSIONS.pop(sender, None)
    admin = cfg.get("admin_group")
    _QUEUE.put({"bot": bot, "admin": admin, "messages": list(msgs),
                "targets": list(targets), "grouping": grouping, "delay": _delays(cfg)})
    reply(chat, f"开始把 {len(msgs)} 条群发到「{grouping}」的 {len(targets)} 个群 🚀\n"
                f"为防风控加了随机延迟，会比较慢，发完我在这儿汇报结果。")
    return True


def _cancel_session(chat, sender) -> bool:
    with _STATE_LOCK:
        _SESSIONS.pop(sender, None)
    reply(chat, "已退出转发模式。")
    return True


def _expire_sessions():
    now = time.time()
    with _STATE_LOCK:
        for s in [s for s, v in _SESSIONS.items() if now - v["last_active"] > SESSION_TTL]:
            _SESSIONS.pop(s, None)
        for s in [s for s, p in _PENDING_MENU.items() if now - p["ts"] > MENU_TTL]:
            _PENDING_MENU.pop(s, None)


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
