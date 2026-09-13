# -*- coding: utf-8 -*-
"""见群打🐶 —— 引擎的④（2026-09-13 按方案 A 重写，提案见
docs/superpowers/specs/2026-09-13-group-tagging-proposal.md §3.1 / §7）。

为什么重写：原来的入口只挂在【独立子窗口】的 friend 回调上，而没写进 config.group 的群
根本没有子窗口 —— 它的消息在主循环里被上游一句「私聊全局监听收到群聊消息，跳过」扔掉，
插件永远看不到。09-11「老友记们」就是活标本：人先 /添加群，15 秒后群里 @ 一句才"发现"，
当场打备注还失败了（从监听线程 ChatWith 主窗口切歪，读到 chat_type=friend）。

现在两条入口，分工明确：
  · handle_global_group —— 全局监听 GetNextNewMessage 读到群消息那一刻（wxbot_core 唯一 hook）。
    主窗口此时正停在该群上，群名 / 类型都在手里，**不 ChatWith**，直接
    confirm_group_window（显示名严格相等 + chat_type=group）→ SetGroupRemark → 回读复核。
    这是整条链路里最便宜也最安全的落点。跑在主循环线程，持 MAIN_WINDOW_LOCK 2–3 秒。
  · handle_discovery —— 独立监听的群（config.group 里的）的 friend 回调。只做登记 + touch，
    **绝不在监听线程里 ChatWith**；要打备注就往 task_runner 写一行「修备注 <群名>」，
    交给 bot 进程后台线程持闸门去做（那条路 ChatWith + 确认 + 回读都是生产验证过的）。

开关（插件 data/config.json 的 discovery 段，默认全关 = 观察模式，见 store.DEFAULT_CONFIG）：
  auto_tag_global / auto_tag_listened / digest_time / max_attempts。

规则（用户 2026-09-13 拍板 1a / 2b / 3b / 4a）：
  · 未命名群（显示名含「、」且不在登记表）不登记、不打，内存记一下当天别重复判，改名后当新群；
  · 监听中的群也自动打（auto_tag_listened），打完 tagsync 同步 config.json 三处 + 复制记忆目录；
  · 管理群（forward._admin_group_names）绝不打；
  · 所有通知走飞书 webhook，不发管理群；
  · 「登记了但没🐶」不再即时提醒，每天一次汇总（digest_tick）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date

from . import audit, registry, store
from .common import log

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_DIR, "data", "discovery_state.json")

DIGEST_TAG = "ncc_tag_digest"
DEFAULT_DIGEST_TIME = "09:00"
# 同一个监听中的群往 task_runner 排「修备注」的最小间隔（秒）：任务 10 秒内就会被取走，
# 这个冷却防的是"打失败了、群里每条消息都再排一次"
REQUEUE_COOLDOWN = 3600

_LOCK = threading.Lock()
_UNNAMED_SEEN: set = set()     # 当天看过的未命名群（「松爸、王伟」这种），别每条消息都判
_UNNAMED_DAY = None
_QUEUED: dict = {}             # 群名 -> 上次排队时间


def _reset_runtime_state() -> None:
    """单测用：清掉进程内状态。"""
    global _UNNAMED_DAY
    with _LOCK:
        _UNNAMED_SEEN.clear()
        _QUEUED.clear()
        _UNNAMED_DAY = None


# ---------------------------------------------------------------- 配置

def settings() -> dict:
    """discovery 段：默认值 + 文件里的覆盖（文件缺段/缺键都能跑）。每次调用重读，改了即生效。"""
    merged = dict(store.DEFAULT_CONFIG.get("discovery") or {})
    try:
        user = (store.load() or {}).get("discovery")
    except Exception:
        user = None
    if isinstance(user, dict):
        merged.update(user)
    try:
        merged["max_attempts"] = max(1, int(merged.get("max_attempts") or 3))
    except (TypeError, ValueError):
        merged["max_attempts"] = 3
    return merged


def _admin_names(cfg) -> set:
    from .forward import _admin_group_names
    return _admin_group_names(cfg)


def _is_admin(name: str, admins: set) -> bool:
    return name in admins or audit.strip_dog(name) in admins


def _listened(bot, name: str) -> bool:
    from .tagsync import is_listened
    return is_listened(bot, name)


def _notify(title: str, content: str) -> bool:
    from .tagsync import notify
    return notify(title, content)


def _panel_url() -> str:
    try:
        from . import panel
        return panel.panel_url()
    except Exception:
        return ""


# ---------------------------------------------------------------- ① 全局监听入口

def handle_global_group(bot, chat_name, messages_new, msgs) -> None:
    """wxbot_core.get_next_new_message 的 hook：全局监听刚读到 chat_type=='group' 的一批消息。
    此刻主窗口停在该群上。旁路逻辑，绝不抛出。"""
    try:
        _handle_global_group(bot, chat_name, messages_new, msgs)
    except Exception as e:
        log("ERROR", f"全局打标出错 {chat_name!r}: {e}")


def _observe_line(name, messages_new, msgs, verdict, detail, mode) -> str:
    attrs, types = [], []
    for m in msgs or []:
        a = str(getattr(m, "attr", "") or "")
        t = str(getattr(m, "type", "") or "")
        if a and a not in attrs:
            attrs.append(a)
        if t and t not in types:
            types.append(t)
    has_remark = isinstance(messages_new, dict) and ("remark" in messages_new)
    tail = f"（{detail}）" if detail and detail != name + audit.DOG else ""
    return (f"[tagging-observe] 群={name} attr={','.join(attrs) or '-'} type={','.join(types) or '-'} "
            f"有remark键={has_remark} 判定={verdict}{tail} 模式={mode}")


def _handle_global_group(bot, chat_name, messages_new, msgs) -> None:
    name = str(chat_name or "").strip()
    if not name:
        return
    cfg = store.load()
    s = settings()
    admins = _admin_names(cfg)
    data = registry.load()
    known = set(data.get("groups", {}))
    overrides = cfg.get("remark_overrides") or {}

    if _is_admin(name, admins):
        verdict, detail = "admin", "管理群，绝不打"
    else:
        verdict, detail = audit.plan_remark(name, known, overrides)
    listened = verdict == audit.FIX_APPLY and _listened(bot, name)
    mode = "自动" if s["auto_tag_global"] else "观察"
    log("INFO", _observe_line(name, messages_new, msgs, verdict,
                              detail + ("；在 config.group 监听中" if listened else ""), mode))

    if not s["auto_tag_global"] or verdict == "admin":
        return                                   # 观察模式：不打、不写登记表

    if verdict == audit.FIX_OK:
        key, _ = registry.find_by_chat_who(data, name)
        if key is None:
            base = audit.strip_dog(name)
            key = base if base in known else None
        if key:
            g = registry.get_group(data, key) or {}
            if not g.get("remark_applied"):
                registry.mark_remark_applied(key, name)
            registry.touch_last_seen(key)
        return

    if verdict == audit.FIX_UNKNOWN:
        # 显示名带🐶但登记表里没有：多半是「修备注 全部」打过却没登记，直接登记成已打
        base = audit.strip_dog(name)
        is_new = base not in known
        registry.add_pending(base)
        registry.mark_remark_applied(base, name)
        if is_new:
            log("INFO", f"发现已带🐶但未登记的群：{name}，已登记为待归类")
            _notify(f"发现已带🐶的新群：{base}",
                    f"群「{name}」在全局监听里冒头，显示名已带🐶但登记表里没有，已登记为待归类"
                    f"（remark_applied=True）。\n去面板归类：{_panel_url()}")
        return

    if verdict == audit.FIX_SKIP and "、" in name and name not in known:
        _note_unnamed(name)
        return

    if verdict in (audit.FIX_SKIP, audit.FIX_CONFLICT):
        _pending_and_notify_once(name, verdict, detail)
        return

    if verdict != audit.FIX_APPLY:
        return

    if listened and not s["auto_tag_listened"]:
        _pending_and_notify_once(
            name, "listened",
            f"这个群在 config.group 里独立监听，而 auto_tag_listened 关着，没有自动打。"
            f"要打：在管理群或 task_request.txt 发「修备注 {name}」（会同步 config.json），"
            f"或把插件 data/config.json 的 discovery.auto_tag_listened 打开。")
        return

    g = registry.get_group(data, name) or registry.add_pending(name)
    attempts = int(g.get("tag_attempts") or 0)
    if attempts >= s["max_attempts"]:
        return                                   # 已叫过人，不再自动试

    from .forward import MAIN_WINDOW_LOCK, _do_set_remark
    t0 = time.time()
    with MAIN_WINDOW_LOCK:
        ok, why = _do_set_remark(bot.wx, name, detail)
    if ok:
        log("INFO", f"见群打🐶成功：{name} -> {detail}（{time.time() - t0:.1f}s）")
        from .tagsync import post_tag
        post_tag(bot, name, detail, source="global")
        return
    n = registry.record_tag_attempt(name, why)
    log("WARNING", f"见群打🐶失败（第 {n} 次）{name}: {why}")
    if n >= s["max_attempts"]:
        _notify(f"群🐶自动打标失败 {n} 次：{name}",
                f"群「{name}」自动打🐶连续 {n} 次没成功，不再自动试，请人工处理。\n"
                f"最后一次：{why}\n"
                f"处置：在微信里确认该群备注为空，然后在管理群发「修备注 {name}」"
                f"（或写进 plugins/ncc_community/data/task_request.txt）。\n"
                f"面板：{_panel_url()}")


def _note_unnamed(name: str) -> None:
    """未命名群：不登记不打，当天只记一次日志。"""
    global _UNNAMED_DAY
    today = date.today().isoformat()
    with _LOCK:
        if _UNNAMED_DAY != today:
            _UNNAMED_SEEN.clear()
            _UNNAMED_DAY = today
        if name in _UNNAMED_SEEN:
            return
        _UNNAMED_SEEN.add(name)
    log("INFO", f"未命名群（显示成员名）「{name}」，不登记不打，改名后再按新群处理")


def _pending_and_notify_once(name: str, kind: str, detail: str) -> None:
    """登记为 pending（不打），同一个群同一种情况只飞书一次（落盘去重，重启不重发）。"""
    registry.add_pending(name)
    if not registry.note_tag_notice(name, kind):
        return
    log("INFO", f"群「{name}」需人工处理（{kind}）：{detail}")
    _notify(f"群🐶需人工：{name}",
            f"群「{name}」在全局监听里冒头，已登记为待归类但【没有】自动打🐶：\n{detail}\n"
            f"面板：{_panel_url()}")


# ---------------------------------------------------------------- ② 独立监听 friend 回调入口

def handle_discovery(bot, chat, msg, cfg) -> None:
    """独立监听的群（config.group）的 friend 消息入口。旁路，不返回处理标志。

    这条路跑在 wxautox 监听线程里，**绝不碰主窗口**（不 ChatWith、不 SetGroupRemark）：
    从子窗口回调里切主窗口就是 09-11 老友记们打标失败的根因。要打备注就排给 task_runner。"""
    chat_type = str(getattr(chat, "chat_type", "") or "")
    if chat_type != "group":
        return
    who = str(getattr(chat, "who", "") or "").strip()
    if not who or _is_admin(who, _admin_names(cfg)):
        return

    data = registry.load()
    key, g = registry.find_by_chat_who(data, who)
    if key is None:
        log("INFO", f"发现新群（监听中）：{who}")
        g = registry.add_pending(who)
        key = who
        _notify(f"发现新群（监听中）：{who}",
                f"独立监听的群「{who}」第一次说话，登记表里没有，已登记为待归类。\n"
                + ("显示名已带🐶。" if audit.has_dog(who) else
                   ("会自动排「修备注」打🐶。" if settings()["auto_tag_listened"]
                    else "auto_tag_listened 关着，不会自动打🐶；要打请发「修备注 " + who + "」。"))
                + f"\n去面板归类：{_panel_url()}")
    else:
        registry.touch_last_seen(key)

    if audit.has_dog(who):
        return
    s = settings()
    if not s["auto_tag_listened"] or not _listened(bot, who):
        return
    if int((g or {}).get("tag_attempts") or 0) >= s["max_attempts"]:
        return
    _enqueue_fix(who)


def _enqueue_fix(who: str) -> bool:
    """往 task_runner 的请求文件写一行「修备注 <群名>」。有请求在排队就不覆盖（下条消息再试）。"""
    from . import task_runner
    now = time.time()
    with _LOCK:
        if now - _QUEUED.get(who, 0.0) < REQUEUE_COOLDOWN:
            return False
        path = task_runner.REQUEST_PATH
        if os.path.exists(path):
            log("INFO", f"后台任务请求文件已有内容，「修备注 {who}」这次不排，等下条消息")
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # newline="" + 无 BOM：BOM 会让指令头上多个不可见字符（task_runner 那边有注释）
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(f"修备注 {who}")
        except OSError as e:
            log("ERROR", f"排「修备注 {who}」失败：{e}")
            return False
        _QUEUED[who] = now
    log("INFO", f"已排后台任务「修备注 {who}」（bot 10 秒内执行）")
    return True


# ---------------------------------------------------------------- ③ 每日汇总

def list_untagged(data: dict, admins=()) -> list:
    """登记表里「没打上🐶」的群：remark_applied=False，或备注串里没有狗。管理群不算。
    返回 [(群名, 原因, status, tag_attempts, tag_last_error)]。"""
    out = []
    for name, g in (data.get("groups") or {}).items():
        if _is_admin(name, set(admins or ())):
            continue
        if not g.get("remark_applied"):
            reason = "remark_applied=false"
        elif not audit.has_dog(str(g.get("remark") or "")):
            reason = f"备注「{g.get('remark')}」里没🐶"
        else:
            continue
        out.append((name, reason, str(g.get("status") or ""),
                    int(g.get("tag_attempts") or 0), str(g.get("tag_last_error") or "")))
    return out


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log("WARNING", f"写 discovery 状态失败：{e}")


def digest_tick(bot=None) -> bool:
    """每天一次：把没打上🐶的群汇总成一条飞书。同一天不重发（日期落盘）。返回是否发了。"""
    try:
        today = date.today().isoformat()
        st = _load_state()
        if st.get("digest_last_date") == today:
            return False
        items = list_untagged(registry.load(), _admin_names(store.load()))
        sent = False
        if items:
            lines = [f"登记表里还有 {len(items)} 个群没打上🐶（每天 1 次汇总）："]
            for name, reason, status, attempts, err in items[:60]:
                extra = f"，自动打标失败 {attempts} 次：{err}" if attempts else ""
                lines.append(f"  - {name}（{status or '?'}，{reason}{extra}）")
            if len(items) > 60:
                lines.append(f"  …另有 {len(items) - 60} 个")
            lines.append("处置：备注为空的群在管理群发「修备注 <群名>」或「修备注 全部」；"
                         "登记表说打过但微信里没有的，去面板核对。")
            lines.append(f"面板：{_panel_url()}")
            sent = _notify(f"群🐶日报：{len(items)} 个群还没打标", "\n".join(lines))
        st["digest_last_date"] = today
        st["digest_count"] = len(items)
        _save_state(st)
        log("INFO", f"群🐶日报：{len(items)} 个未打标{'，已发飞书' if sent else ''}")
        return sent
    except Exception as e:
        log("ERROR", f"群🐶日报出错：{e}")
        return False


def register_digest(bot, schedule) -> None:
    """挂进 bot 的 schedule（由 task_runner.register 顺带调用，不在 wxbot_core 另加 hook）。"""
    t = str(settings().get("digest_time") or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        log("WARNING", f"discovery.digest_time「{t}」不是 HH:MM，用默认 {DEFAULT_DIGEST_TIME}")
        t = DEFAULT_DIGEST_TIME
    schedule.clear(DIGEST_TAG)
    schedule.every().day.at(t).do(digest_tick, bot).tag(DIGEST_TAG)
    log("INFO", f"群🐶日报已挂载：每天 {t} 汇总未打标的群到飞书")
