# -*- coding: utf-8 -*-
"""管理群转发与管理指令 —— 引擎的②。

交互习惯对齐旧 WCRobot（ncc/ncc_manager.py）：发「ncc」弹主菜单，数字选择。

  ncc              -> 主菜单：1转发 / 2同步 / 3后台 / 4待归类 / 5迎新拉群 / 0退出
  1                -> 进入转发：把消息【一条条发完】
  1                -> 收集完，进入选分组
  2+4+6            -> 多选分组编号（1=所有群聊），后台群发
  0                -> 随时退出

投递沿用旧模型（照搬 ncc_manager 的 _process_forward_queue）：
- 一群一群发（`msg.forward(单个群)`），绕开微信"多选转发≤9"上限；
- 防风控延迟：群间 3-5s、每 10 群额外 5-10s、消息间 1-2s、重试 3 次；
- 后台线程跑，收集/接收不阻塞。
- 视频号是 type='other'，会正常收集与转发；真转不了的连续 2 群失败即跳过并汇报。

分组/权限来自 Notion（同步到 registry），发「同步」拉取。状态按发送人隔离。
"""
from __future__ import annotations

import re
import time
import random
import threading
from queue import Queue

from . import store, registry
from .common import REPLY_PREFIX, is_bot_reply, log, reply

NOTION_BACKEND_URL = "https://bigsong.notion.site/NCC-1564e93f5682805d9a2ff0519c24738b"

# ---- 每个操作者的状态机（对齐旧 OperatorState）----
S_MAIN = "main"            # 主菜单
S_FWD_COLLECT = "collect"  # 转发-收集消息
S_FWD_CHOOSE = "choose"    # 转发-选分组
_STATE = {}                # sender -> {"state","messages":[...],"last_active"}
_STATE_LOCK = threading.Lock()
STATE_TTL = 600            # 状态闲置超时（秒）

# 主窗口操作锁：每次 forward 单独加锁，间隔延迟时释放，让监听线程插空收消息
MAIN_WINDOW_LOCK = threading.Lock()

DELAY = {  # 防风控延迟默认值（照搬旧 WCRobot），可被 config.forward.delay 覆盖
    "group_min": 3.0, "group_max": 5.0,
    "batch_every": 10, "batch_min": 5.0, "batch_max": 10.0,
    "msg_min": 1.0, "msg_max": 2.0, "max_retries": 3,
}

# 收集时忽略的消息类型：只跳真噪音。视频号是 'other'，必须收集。
_SKIP_COLLECT_TYPES = {"time", "system"}

MAIN_MENU = (
    "🐶 NCC 社群管理（本群成员皆管理员）\n"
    "请回复数字：\n"
    "1 👈 转发消息\n"
    "2 👈 同步 Notion 更改\n"
    "3 👈 查看 Notion 后台\n"
    "4 👈 待归类新群\n"
    "5 👈 迎新 / 拉群设置\n"
    "0 👈 退出管理模式"
)

COLLECT_PROMPT = (
    "请发送需要转发的内容，支持公众号、推文、视频号、文字、图片、合并消息，一个一个来\n"
    "（收集时我不逐条回复，放心连着发）\n"
    "发完回复【1】进入下一步\n"
    "随时发送【0】退出转发模式"
)

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
    for _ in range(int(d["max_retries"])):
        try:
            with MAIN_WINDOW_LOCK:
                r = msg.forward(group)          # 单目标，绕开 9 限制
            if r is None or r:                  # wxautox 成功返回 None
                return True, ""
            last_err = _wxresponse_message(r)
        except Exception as e:
            last_err = str(e)
        time.sleep(2)
    return False, last_err


def _forward_worker():
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


PROMPT_MARK = "请发送需要转发的内容"   # 收集起点边界（COLLECT_PROMPT 里的唯一子串）
_CMD_RE = re.compile(r"^\d{1,2}(\s*\+\s*\d{1,2})*$")   # 只排 0-99 的菜单/多选数字，不误伤长数字内容
GATHER_SETTLE = 1.2                    # 切到源群后等 UI 稳定的秒数（测试里置 0）


def _gather_content(bot, source, operator):
    """【核心】不依赖实时监听：把源群拉出来 GetAllMessage，读取 operator 在本次收集
    起点（最后一条 COLLECT_PROMPT）之后发的所有内容消息（新鲜 UI 元素）。

    这样无论 wxauto 实时 listener 漏没漏、有没有滚出可见区，只要消息还在群里渲染，
    就能读到并转发。返回新鲜的 msg 列表。找不到收集起点则返回 []（宁可不发也不错发）。
    """
    try:
        with MAIN_WINDOW_LOCK:
            bot.wx.ChatWith(source, exact=False)
            time.sleep(GATHER_SETTLE)
            msgs = bot.wx.GetAllMessage() or []
    except Exception as e:
        log("ERROR", f"读取源群消息失败: {e}")
        return []
    # 找最后一次收集起点
    start = None
    for i, m in enumerate(msgs):
        if str(getattr(m, "attr", "")) == "self" and PROMPT_MARK in str(getattr(m, "content", "")):
            start = i
    if start is None:
        log("WARNING", "未找到收集起点(COLLECT_PROMPT)，本次不转发")
        return []
    out = []
    for m in msgs[start + 1:]:
        if str(getattr(m, "attr", "")) != "friend":
            continue
        if operator and str(getattr(m, "sender", "")) != operator:
            continue
        if str(getattr(m, "type", "")) in _SKIP_COLLECT_TYPES:
            continue
        c = str(getattr(m, "content", "") or "").strip()
        if _CMD_RE.match(c):        # 排除 1 / 99 / 2+4 这类指令数字
            continue
        out.append(m)
    log("INFO", f"从源群现读到 {len(out)} 条待转发内容（operator={operator}）")
    return out


def _deliver(task) -> dict:
    """逐群逐条投递（可同步调用便于测试）。带防风控延迟，完成后汇报。"""
    bot = task["bot"]; admin = task["admin"]
    targets = task["targets"]
    label = task["label"]; d = task["delay"]
    # 关键：投递前从源群【现读】新鲜消息（不依赖实时监听、规避失效）
    messages = _gather_content(bot, admin, task.get("operator"))
    if not messages:
        _worker_report(bot, admin, f"{REPLY_PREFIX} 没读到要转发的内容（可能消息已滚动太远或收集起点丢失），"
                                   f"请重新 ncc→1 转发。")
        return {"ok": 0, "fail": 0, "dead": []}

    ok = fail = 0
    dead_msgs = set()
    fail_streak = {}
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
                if fail_streak[mi] >= 2:
                    dead_msgs.add(mi)
            time.sleep(random.uniform(d["msg_min"], d["msg_max"]))
        time.sleep(random.uniform(d["group_min"], d["group_max"]))

    lines = [f"{REPLY_PREFIX} 转发完成！{label}",
             f"成功 {ok} 条次，失败 {fail} 条次，目标 {len(targets)} 个群 × {len(messages)} 条。"]
    if dead_msgs:
        nums = "、".join(f"第{i+1}条" for i in sorted(dead_msgs))
        lines.append(f"⚠️ {nums} 全程转发失败并已跳过（wxauto 无法转发的类型）。")
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


# ------------------------------------------------------------------ 消息入口（状态机）

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

    _expire_states()
    st = _get_state(sender)
    text = content.strip() if mtype == "text" else ""

    # 「ncc」随时可唤出主菜单
    if text.lower() in ("ncc", "菜单", "管理"):
        _set_state(sender, S_MAIN)
        reply(chat, MAIN_MENU)
        return True

    # 有状态时按状态机走
    if st is not None:
        if text == "0":
            _clear_state(sender)
            reply(chat, "已退出管理模式")
            return True
        if st["state"] == S_MAIN:
            return _handle_main_menu(bot, chat, cfg, sender, text)
        if st["state"] == S_FWD_COLLECT:
            return _handle_collect(bot, chat, cfg, sender, st, msg, mtype, text)
        if st["state"] == S_FWD_CHOOSE:
            return _handle_choose(bot, chat, cfg, sender, st, text)

    # 无状态：兼容老的直接文本指令（power user 快捷方式）
    if mtype == "text":
        return _try_direct_command(bot, chat, cfg, sender, text)
    return False


# ------------------------------------------------------------------ 主菜单

def _handle_main_menu(bot, chat, cfg, sender, text) -> bool:
    if text == "1":       # 转发消息
        _set_state(sender, S_FWD_COLLECT, messages=[])
        reply(chat, COLLECT_PROMPT)
        return True
    if text == "2":       # 同步 Notion
        _do_sync(chat)
        reply(chat, MAIN_MENU)
        return True
    if text == "3":       # 查看 Notion 后台
        reply(chat, "Notion 后台（登录查看）：\n" + NOTION_BACKEND_URL)
        return True
    if text == "4":       # 待归类新群
        reply(chat, _format_pending())
        return True
    if text == "5":       # 迎新 / 拉群设置
        reply(chat, _format_welcome_invite(cfg))
        return True
    reply(chat, "请输入有效的选项，或发送【0】退出。\n\n" + MAIN_MENU)
    return True


# ------------------------------------------------------------------ 转发：收集

def _handle_collect(bot, chat, cfg, sender, st, msg, mtype, text) -> bool:
    if text == "1":       # 收集完，进入选分组
        # 不依赖实时监听：从源群现读 operator 本次发的所有内容
        source = cfg.get("admin_group")
        content = _gather_content(bot, source, sender)
        if not content:
            reply(chat, "还未读到任何要转发的消息，请先发送需要转发的内容（发完再回复【1】）")
            return True
        _set_state(sender, S_FWD_CHOOSE, count=len(content))
        reply(chat, _choose_menu(len(content)))
        return True
    # 收集阶段【全程静默】——收不收得到都不影响：真正内容在发「1」时从群里现读。
    st["last_active"] = time.time()
    return True


def _choose_menu(n_msgs) -> str:
    data = registry.load()
    groupings = registry.forward_groupings_detailed(data)
    lines = [f"已收集 {n_msgs} 条消息",
             "请选择想要转发的分组编号（支持多选，如：2+4+6），按0退出：",
             "1 👈 所有群聊"]
    for name, num, cnt in groupings:
        lines.append(f"{num} 👈 {name}（{cnt} 群）")
    if not groupings:
        lines.append("（暂无分组，先发「同步」从 Notion 拉取）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 转发：选分组 + 入队

def _handle_choose(bot, chat, cfg, sender, st, text) -> bool:
    if not re.fullmatch(r"\s*\d+(\s*\+\s*\d+)*\s*", text or ""):
        reply(chat, "请输入有效的选项（支持多选，如：2+4+6），或发送【0】退出转发模式")
        return True
    nums = [int(x) for x in re.split(r"\s*\+\s*", text.strip())]
    data = registry.load()

    targets, label = [], ""
    if 1 in nums:  # 所有群聊
        targets = registry.all_forward_targets(data)
        label = "分组「所有群聊」"
    else:
        chosen, seen = [], set()
        for num in nums:
            gname = registry.grouping_name_by_number(data, num)
            if not gname:
                continue
            chosen.append(gname)
            for t in registry.targets_for_grouping(data, gname):
                if t not in seen:
                    seen.add(t)
                    targets.append(t)
        if not chosen:
            reply(chat, "这些编号都没对应到分组，请重新选择，或发送【0】退出")
            return True
        label = "分组「" + "、".join(chosen) + "」"

    if not targets:
        reply(chat, "未找到任何可转发的群组（检查 Notion 里群是否勾了「允许转发」），或发送【0】退出")
        return True

    count = st.get("count", 0)
    _clear_state(sender)
    # 不传消息对象：worker 投递前从源群现读 operator 的内容（新鲜元素、不漏不失效）
    _QUEUE.put({"bot": bot, "admin": cfg.get("admin_group"), "operator": sender,
                "targets": list(targets), "label": label, "delay": _delays(cfg)})
    reply(chat, f"开始转发 {count} 条消息到 {len(targets)} 个群…\n"
                f"为避免风控，将会添加随机延迟，请耐心等待，完成后我在这儿汇报。")
    return True


# ------------------------------------------------------------------ 状态工具

def _get_state(sender):
    with _STATE_LOCK:
        return _STATE.get(sender)


def _set_state(sender, state, **extra):
    with _STATE_LOCK:
        s = _STATE.get(sender) or {}
        s["state"] = state
        s["last_active"] = time.time()
        for k, v in extra.items():
            s[k] = v
        s.setdefault("messages", [])
        _STATE[sender] = s
        return s


def _clear_state(sender):
    with _STATE_LOCK:
        _STATE.pop(sender, None)


def _expire_states():
    now = time.time()
    with _STATE_LOCK:
        for s in [k for k, v in _STATE.items() if now - v.get("last_active", now) > STATE_TTL]:
            _STATE.pop(s, None)


def _wxresponse_message(result) -> str:
    try:
        return str(result["message"])
    except Exception:
        return str(result)


# ------------------------------------------------------------------ 直接文本指令（无状态快捷方式）

def _try_direct_command(bot, chat, cfg, sender, text) -> bool:
    if not text:
        return False
    plain = text.replace(" ", "")
    if plain in ("帮助", "指令", "转发"):
        _set_state(sender, S_MAIN); reply(chat, MAIN_MENU); return True
    if plain in ("同步", "拉取", "刷新"):
        return _do_sync(chat)
    if plain in ("分组列表", "转发分组"):
        reply(chat, _format_groupings()); return True
    if plain in ("待归类", "新群"):
        reply(chat, _format_pending()); return True
    if plain == "拉群列表":
        reply(chat, _format_invites(cfg)); return True
    if plain == "迎新列表":
        reply(chat, _format_welcomes(cfg)); return True

    m = re.match(r"^(检查群组|开迎新|关迎新|删拉群)\s*(.+)$", text, re.S)
    if m:
        name = m.group(2).strip()
        return {
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


# ------------------------------------------------------------------ 同步 / 查看

def _do_sync(chat) -> bool:
    try:
        from . import notion_sync
        stat = notion_sync.pull()
        reply(chat, f"同步成功 ✅ 分组 {stat['groupings']} 个、群 {stat['groups']} 个"
                    f"（允许转发 {stat['forward_on']} 个）")
    except Exception as e:
        reply(chat, f"同步失败：{e}")
        log("ERROR", f"Notion 同步失败: {e}")
    return True


def _format_groupings() -> str:
    data = registry.load()
    groupings = registry.forward_groupings_detailed(data)
    if not groupings:
        return "还没有可转发的分组。发「同步」从 Notion 拉取。"
    lines = [f"转发分组（共 {len(groupings)} 个，编号即选择号）："]
    for name, num, cnt in groupings:
        lines.append(f"  {num} 👈 {name}（{cnt} 群）")
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


def _format_welcome_invite(cfg) -> str:
    return ("迎新 / 拉群设置：\n"
            "【迎新】在 Notion『群聊列表』填「迎新推送链接」即开启该群迎新卡片。\n"
            "  文案：设迎新文案 <群名>|<文案>（{name}=新人昵称）\n"
            "  开关：开迎新 <群名> / 关迎新 <群名>；查看：迎新列表\n"
            "【拉群】设拉群 <关键词>|<目标群>；删拉群 <关键词>；查看：拉群列表\n"
            "回复 0 退出管理模式。")


def _check_groups(bot, chat, name) -> bool:
    data = registry.load()
    if name in ("全部", "所有", "all"):
        targets = registry.all_forward_targets(data)
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


# ------------------------------------------------------------------ 拉群 / 迎新（config.json）

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
