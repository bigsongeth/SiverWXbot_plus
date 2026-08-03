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

from . import store, registry, audit
from .common import REPLY_PREFIX, is_bot_reply, log, reply

NOTION_BACKEND_URL = "https://bigsong.notion.site/NCC-1564e93f5682805d9a2ff0519c24738b"

# ---- 每个操作者的状态机（对齐旧 OperatorState）----
S_MAIN = "main"            # 主菜单
S_FWD_COLLECT = "collect"  # 转发-收集消息
S_FWD_CHOOSE = "choose"    # 转发-选分组
_STATE = {}                # sender -> {"state","messages":[...],"last_active"}
_STATE_LOCK = threading.Lock()
STATE_TTL = 600            # 状态闲置超时（秒）

# 主窗口全局闸门：转发线程 / 主循环 / 监听线程共用。转发进行时其它任务让路排队，
# 转发做完再按序执行。主循环 + 监听线程侧 hook 见 wxbot_core.py +
# AI_COLLABORATION_GUIDE.md「潜在冲突：主窗口串行闸门」。
from .wxlock import WX_LOCK as MAIN_WINDOW_LOCK
from .wxlock import set_forwarding

# 转发策略：【一个群一个群转】。多群"分别发送"多选框会把微信卡死
# （2026-07-09 实证：转 106 群时微信直接未响应）。单个转发走轻量的"发送给"对话框，稳。
DELAY = {  # 防风控/防卡死延迟（可被 config.forward.delay 覆盖）
    "group_min": 2.5, "group_max": 4.5,     # 每个群之间
    "msg_min": 5.0, "msg_max": 8.0,         # 每条消息之间
    "batch_every": 10, "batch_min": 5.0, "batch_max": 9.0,   # 每 N 群额外歇一会儿
    "max_retries": 2,
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
    "0 👈 退出管理模式\n"
    "\n体检指令（直接发）：\n"
    "  检查群组 全部 —— 群还在不在（可达性）\n"
    "  核对备注 全部 —— 🐶备注有没有打错群\n"
    "  扫群 —— 微信里到底有多少个群\n"
    "  修备注 预览 / 修备注 全部 —— 把每个群的备注修成「群名🐶」并回写 Notion"
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


def _switched(wx, who: str, exact: bool = False) -> bool:
    """切到某个会话，并【接住返回值】判断到底切成功没有。

    ChatWith 找不到会话是静默失败（返回 falsy WxResponse，不抛异常）。不看返回值就继续
    操作，等于在"上一个还开着的窗口"上干活：读到别人的消息、把备注/成员改到别的会话上
    （2026-07-30 拉群故障的根因就是这个，详见 plugins/ncc_community/invite.py 顶部）。
    wxautox 也有成功返回 None 的先例，所以 None 视为成功。"""
    try:
        r = wx.ChatWith(who=who, exact=exact)
    except Exception as e:
        log("WARNING", f"切到「{who}」抛异常：{e}")
        return False
    if r is None or r:
        return True
    log("WARNING", f"切到「{who}」失败：{_wxresponse_message(r)}")
    return False


def _after_collect_start(msgs):
    """只保留本次收集起点（COLLECT_PROMPT）之后的消息。

    定位靠签名，而签名对图片这类消息是【不唯一】的（wxautox 里图片消息的 content
    固定就是「图片」，两张不同的图签名完全一样，只能靠"第几条"区分）。不截断的话，
    更早那次转发留下的同款消息会把序号顶偏，第 2 张图定位到别的图上。
    找不到起点就原样返回（退回旧行为，不比以前差）。"""
    start = -1
    for i, m in enumerate(msgs):
        if str(getattr(m, "attr", "")) == "self" and PROMPT_MARK in str(getattr(m, "content", "")):
            start = i
    return msgs[start + 1:] if start >= 0 else msgs


def _locate(bot, source, sig, occ=0):
    """（须在 MAIN_WINDOW_LOCK 内调用）滚动定位到 sig 对应的【第 occ 条】消息，返回它
    当前可见的新鲜元素或 None。ChatWith 回最新 → GetHistoryMessage 往上滚，匹配到本条
    签名就停，视图停在它上面 → GetAllMessage 取此刻可见（有效）的同签名元素。
    occ 是同签名消息里的序号（0 起）：连发两张图签名相同，只能按序号区分。
    可见范围里凑不够 occ+1 条时【宁可返回 None】（报定位失败、不标记群），
    也不退而求其次拿别的元素——那会把同一张图往 100 多个群里重复发一遍。"""
    STOP = _stop_sign()
    if not _switched(bot.wx, source):
        # 切不过去就别在别人的窗口里翻消息（ChatWith 静默失败，返回 falsy 不抛异常）
        log("WARNING", f"定位消息前切到源「{source}」失败，本次放弃定位")
        return None
    time.sleep(GATHER_SETTLE)

    def cb(m, _s=sig):
        if _sig(m) == _s:
            return STOP

    try:
        bot.wx.GetHistoryMessage(n=120, callback=cb, goback=False)
    except TypeError:
        bot.wx.GetHistoryMessage(n=120, callback=cb)
    except Exception:
        pass
    cands = [m for m in _after_collect_start(bot.wx.GetAllMessage() or []) if _sig(m) == sig]
    if len(cands) > occ:
        return cands[occ]
    if cands:
        log("WARNING", f"同签名消息可见 {len(cands)} 条，取不到第 {occ + 1} 条（{sig[0]}），本条判定位失败")
    return None


# "群不存在/无结果"错误特征（被踢/解散/改名，备注搜不到）
_GONE_HINTS = ("无结果", "找不到", "未找到", "不存在", "no result", "not found", "无搜索结果")
_STALE_HINTS = ("已失效", "失效", "invalid", "无效")


def _is_gone(err: str) -> bool:
    e = (err or "").lower()
    return any(h.lower() in e for h in _GONE_HINTS)


def _is_stale(err: str) -> bool:
    e = (err or "").lower()
    return any(h.lower() in e for h in _STALE_HINTS)


def _forward_one_shot(cache_box, bot, source, sig, occ, group, d) -> tuple[bool, str]:
    """把定位到的消息转发给【单个群】（走轻量的"发送给"对话框，不碰会卡死微信的
    "分别发送"多选框）。stale 才重定位重试；无结果/失败不重试（单群要么成要么就是没了）。
    返回 (成功, 错误)。"""
    last = ""
    for _ in range(int(d["max_retries"])):
        with MAIN_WINDOW_LOCK:
            if cache_box[0] is None:
                cache_box[0] = _locate(bot, source, sig, occ)
            if cache_box[0] is None:
                last = "视图中定位不到该消息"
            else:
                try:
                    try:
                        cache_box[0].roll_into_view()
                    except Exception:
                        pass
                    r = cache_box[0].forward(group)   # 单目标字符串
                    if r is None or r:
                        return True, ""
                    return False, _wxresponse_message(r) or "无结果"   # 单群失败 = 该群没了，不重试
                except Exception as e:
                    last = str(e)
                    if _is_stale(last):
                        cache_box[0] = None           # 元素失效 → 重定位重试
                    else:
                        return False, last            # 其它错误（多为无结果）→ 不重试
        time.sleep(1.5)
    return False, last


def _forward_located_message(bot, source, sig, occ, targets, d):
    """把一条消息（按签名+序号定位）【一个群一个群】转发。无结果的群记下来，其余照发。
    每 batch_every 个群额外歇一会儿。返回 (成功群数, 无结果群列表, 其它失败[str])。"""
    cache_box = [None]
    ok = 0
    gone = []
    failed = []
    for i, g in enumerate(targets):
        if i > 0 and i % int(d["batch_every"]) == 0:
            time.sleep(random.uniform(d["batch_min"], d["batch_max"]))
        success, err = _forward_one_shot(cache_box, bot, source, sig, occ, g, d)
        if success:
            ok += 1
        elif "定位不到该消息" in err:
            failed.append(f"{g}: {err}")            # 源消息定位不到，非群的问题
        elif _is_gone(err):
            gone.append(g)                          # 明确"无结果/找不到" → 该群不可达
        else:
            # 超时、UI 异常这类说不清的错，只汇报不下结论：gone 会被 mark_unreachable
            # 写进 registry（allow_forward=False），之后所有转发都跳过这个群，
            # 没人去 Notion 核对就是永久漏发。宁可少标一个，别冤枉一个。
            failed.append(f"{g}: {err}")
        time.sleep(random.uniform(d["group_min"], d["group_max"]))

    # 保护：整条一个群都没成功 → 判定是这条消息本身转不了，别冤枉群（不标记任何群不可达）
    if ok == 0 and (gone or failed):
        return 0, [], ["整条转发失败（该消息可能不支持转发，未标记任何群）"]
    return ok, gone, failed


def _forward_worker():
    while True:
        task = _QUEUE.get()
        try:
            if task:
                # 举起"转发中"闸门：整个群发期间主循环/监听线程让路排队，做完再按序处理
                set_forwarding(True)
                try:
                    _deliver(task)
                finally:
                    set_forwarding(False)
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


def _stop_sign():
    try:
        from wxautox4 import WxParam
        return WxParam.CALLBACK_STOP_SIGN
    except Exception:
        return "stop"


def _read_source_messages(bot, source, goback=True):
    """在主窗口用 GetHistoryMessage【往上滑动加载】源群消息，滑到本次收集起点
    （COLLECT_PROMPT）就停。这样即使消息多、超出可见区，也会被滚动加载出来。
    goback=False 时结束后停在收集起点（便于随后从上往下逐条转发，保持元素有效）。"""
    with MAIN_WINDOW_LOCK:
        if not _switched(bot.wx, source):
            log("WARNING", f"读取源群前切到「{source}」失败，本次读到 0 条")
            return []
        time.sleep(GATHER_SETTLE)
        STOP = _stop_sign()

        def cb(m):
            if str(getattr(m, "attr", "")) == "self" and PROMPT_MARK in str(getattr(m, "content", "")):
                return STOP   # 滑到收集起点，停止上滑

        try:
            return bot.wx.GetHistoryMessage(n=100, callback=cb, goback=goback) or []
        except TypeError:
            # 老签名不支持 goback
            try:
                return bot.wx.GetHistoryMessage(n=100, callback=cb) or []
            except Exception as e:
                log("WARNING", f"GetHistoryMessage 失败，回退 GetAllMessage: {e}")
                return bot.wx.GetAllMessage() or []
        except Exception as e:
            log("WARNING", f"GetHistoryMessage 失败，回退 GetAllMessage: {e}")
            try:
                return bot.wx.GetAllMessage() or []
            except Exception:
                return []


def _gather_content(bot, source, operator, goback=True):
    """【核心】不依赖实时监听：主窗口上滑加载到收集起点，取 operator 本次发的所有
    内容消息。无论实时 listener 漏没漏、有没有滚出可见区，都能拿全。
    goback=False：读完停在收集起点（投递时用，随后逐条 roll+转，元素保持有效）。"""
    try:
        msgs = _read_source_messages(bot, source, goback=goback)
    except Exception as e:
        log("ERROR", f"读取源群消息失败: {e}")
        return []

    has_prompt = any(str(getattr(m, "attr", "")) == "self" and PROMPT_MARK in str(getattr(m, "content", ""))
                     for m in msgs)
    log("INFO", f"gather 上滑读到 {len(msgs)} 条；收集起点={'找到' if has_prompt else '未找到'}")

    # GetHistoryMessage 已按收集起点截断，本次范围内 operator 的内容全取；
    # 未找到起点则回退取末尾最近，避免漏发。
    scope = msgs if has_prompt else msgs[-20:]
    out = []
    for m in scope:
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


def _sig(msg):
    return (str(getattr(msg, "type", "")), str(getattr(msg, "content", ""))[:60])


def _msg_uid(msg):
    """消息在【同一份列表】里的唯一标识（wxautox Message 自带 id）。只用于去重，
    不要求跨次调用稳定。取不到返回 None，调用方退回按签名去重。"""
    uid = getattr(msg, "id", None)
    if uid in (None, ""):
        return None
    return str(uid)


def _deliver(task) -> dict:
    """先拿本次要转消息的【签名清单】(身份)，再【逐条】：滚动定位到该消息、取当下
    有效元素、转发给各批群。每条只在"可见"时才转，规避元素失效。按时序逐条处理。
    主窗口锁已和主循环共用，转发期间主循环不会来抢窗口。
    """
    bot = task["bot"]; admin = task["admin"]
    targets = task["targets"]
    label = task["label"]; d = task["delay"]
    operator = task.get("operator")

    # 拿签名清单（只需身份；GetHistoryMessage 会把全部滚出来，即便元素随后失效也不影响取签名）
    manifest = _gather_content(bot, admin, operator, goback=True)
    if not manifest:
        _worker_report(bot, admin, f"{REPLY_PREFIX} 没读到要转发的内容（收集起点可能丢失），请重新 ncc→1。")
        return {"ok": 0, "fail": 0, "dead": []}
    # 去重保序。⚠️ 不能按签名去重：图片消息的 content 在 wxautox 里固定是「图片」，
    # 连发两张图签名一模一样，按签名去重会把第二张吞掉（只转第一张，用户以为都发了）。
    # 所以按消息 id 去重（同一份列表里 id 唯一），再给同签名的消息编序号 occ，
    # 投递时按序号定位第几条。没有 id 的旧版/假对象退回按签名去重（旧行为）。
    sigs = []                          # [(sig, occ)]
    seen_uid, seen_sig, counter = set(), set(), {}
    for m in manifest:
        s = _sig(m)
        uid = _msg_uid(m)
        if uid is not None:
            if uid in seen_uid:
                continue
            seen_uid.add(uid)
        else:
            if s in seen_sig:
                continue
            seen_sig.add(s)
        sigs.append((s, counter.get(s, 0)))
        counter[s] = counter.get(s, 0) + 1
    n_msgs = len(sigs)

    ok = fail = 0
    gone_all = set()          # 无结果/不可达的群（去重）
    fail_detail = []

    for mi, (sig, occ) in enumerate(sigs):
        okc, gone, failed = _forward_located_message(bot, admin, sig, occ, targets, d)
        ok += okc
        fail += len(gone) + len(failed)
        gone_all.update(gone)
        if failed:
            fail_detail.extend(f"第{mi+1}条 {x}" for x in failed[:5])
        time.sleep(random.uniform(d["msg_min"], d["msg_max"]))

    # 无结果的群 → 本地标记不可达（后续转发自动跳过），并从 Notion 视角提醒人清理
    marked = []
    for g in gone_all:
        try:
            name = registry.mark_unreachable(g)
            marked.append(name or g)
        except Exception as e:
            log("WARNING", f"标记不可达群失败 {g}: {e}")

    # 收尾：主窗口回到最新
    try:
        with MAIN_WINDOW_LOCK:
            bot.wx.ChatWith(admin, exact=False)
    except Exception:
        pass

    lines = [f"{REPLY_PREFIX} 转发完成！{label}",
             f"成功 {ok} 条次，失败 {fail} 条次，目标 {len(targets)} 个群 × {n_msgs} 条。"]
    if marked:
        lines.append(f"🚫 {len(marked)} 个群转发无结果（可能被踢/解散/改名），已本地标记跳过："
                     + "、".join(marked[:10]) + ("…" if len(marked) > 10 else "")
                     + "\n（记得去 Notion 里核对这些群）")
    if fail_detail:
        show = fail_detail[:10]
        lines.append("其它失败：\n" + "\n".join(show) + ("…" if len(fail_detail) > 10 else ""))
    _worker_report(bot, admin, "\n".join(lines))
    return {"ok": ok, "fail": fail, "gone": sorted(gone_all)}


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

    if plain in ("扫群", "扫描群列表"):
        return _scan_groups(bot, chat)

    m = re.match(r"^(检查群组|核对备注|修备注|看群|看会话|探面板|开迎新|关迎新|删拉群)\s*(.+)$", text, re.S)
    if m:
        name = m.group(2).strip()
        return {
            "检查群组": lambda: _check_groups(bot, chat, name),
            "核对备注": lambda: _audit_remarks(bot, chat, name),
            "修备注": lambda: _fix_remarks(bot, chat, name),
            "看群": lambda: _inspect_chat(bot, chat, name),
            "看会话": lambda: _inspect_sessions(bot, chat, name),
            "探面板": lambda: _inspect_panel(bot, chat, name),
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
        msg = (f"同步成功 ✅ 分组 {stat['groupings']} 个、群 {stat['groups']} 个"
               f"（允许转发 {stat['forward_on']} 个）、拉群关键词 {stat.get('invites', 0)} 条")
        # 改名迁移不能静默：寻址仍走微信里的老备注，人得知道表里名字变了
        renamed = stat.get("renamed") or []
        if renamed:
            msg += f"\n检测到 {len(renamed)} 个群在 Notion 改了名（寻址仍用原备注，不影响转发）："
            msg += "".join(f"\n  「{r['from']}」→「{r['to']}」" for r in renamed)
        reply(chat, msg)
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
            "【拉群】关键词维护在 Notion『迎新拉群』表，发「同步」后生效；查看：拉群列表\n"
            "  本地覆盖：设拉群 <关键词>|<目标群>；删拉群 <关键词>\n"
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
        # 以前只捕异常不看返回值，切群静默失败会被报成"可达"，检查等于白做
        with MAIN_WINDOW_LOCK:
            reachable = _switched(wx, t, exact=False)
        (ok_list if reachable else fail_list).append(t)
        time.sleep(0.5)
    lines = [f"检查完成：{len(ok_list)}/{len(targets)} 可达。"]
    if fail_list:
        lines.append("不可达（可能改了群名/退群，去 Notion 更新）：")
        lines.extend(f" - {t}" for t in fail_list)
    reply(chat, "\n".join(lines))
    return True


def _select_groups(data, scope):
    """按范围选出 [(群名, 群条目)]。scope = 全部 / 分组名。选不出时返回 None。"""
    groups = data.get("groups", {})
    if scope in ("全部", "所有", "all"):
        return sorted(groups.items())
    if scope in data.get("groupings", {}):
        return sorted((n, g) for n, g in groups.items() if scope in (g.get("groupings") or []))
    return None


def _probe_remark(wx, query):
    """切到 query 并读回当前窗口信息。切不过去 / 读不到都返回 None。"""
    if not _switched(wx, query, exact=False):
        return None
    try:
        info = wx.ChatInfo() or {}
    except Exception as e:
        log("WARNING", f"核对备注读窗口信息失败「{query}」：{e}")
        return None
    return info if isinstance(info, dict) and info else None


def _audit_remarks(bot, chat, scope) -> bool:
    """核对「群名🐶」这个备注到底挂在谁头上——查 A 的备注被打到 B 上的情况。

    不能复用「检查群组」：错打时 ChatWith 是【成功】的（切到了被错打的那个群），
    可达性检查一律报 ✅，错打对它完全隐形。这里改成拿备注串去搜、再读回真实群名比对。
    """
    data = registry.load()
    items = _select_groups(data, scope)
    if items is None:
        reply(chat, f"没有「{scope}」这个分组。发「分组列表」看看，或用「核对备注 全部」。")
        return True
    if not items:
        reply(chat, "该分组下没有群可核对。")
        return True

    known = set(data.get("groups", {}))
    reply(chat, f"开始核对 {len(items)} 个群的备注归属，逐个切窗口，大约 {max(1, len(items) * 2 // 60)} 分钟…")
    wx = getattr(bot, "wx", None)
    results = []
    for name, _g in items:
        with MAIN_WINDOW_LOCK:
            info = _probe_remark(wx, name + audit.DOG)
        verdict, detail = audit.classify(name, info, known)
        results.append((name, verdict, detail))
        if verdict in (audit.MISAPPLIED, audit.NOT_GROUP):
            log("WARNING", f"备注核对：{detail}")
        time.sleep(0.5)
    reply(chat, audit.summarize(results))
    return True


def _scan_groups(bot, chat) -> bool:
    """「扫群」：只调 GetAllRecentGroups 看一眼返回结构，不碰任何备注。

    wxautox 是编译发行的，文档只写了 `List[Tuple]`、tuple 里是什么读不到源码，
    所以先用这条只读指令把结构实测确认掉，再让「修备注」动手。"""
    wx = getattr(bot, "wx", None)
    reply(chat, "开始扫描微信里的所有群（要滑一遍会话列表），稍等…")
    t0 = time.time()
    try:
        with MAIN_WINDOW_LOCK:
            raw = wx.GetAllRecentGroups()
    except Exception as e:
        reply(chat, f"扫描失败：{e}")
        log("ERROR", f"GetAllRecentGroups 失败：{e}")
        return True
    desc = audit.describe_raw(raw)
    log("INFO", f"扫群耗时 {time.time() - t0:.1f}s\n{desc}")
    reply(chat, f"耗时 {time.time() - t0:.1f} 秒。\n{desc}")
    return True


def _inspect_chat(bot, chat, arg) -> bool:
    """「看群 A|B」：切过去把 ChatInfo() 的完整返回打出来。只读，不改任何东西。

    存在的理由：2026-08-03 「修备注 预览」实测发现 ChatInfo 的 chat_name 给的是
    【当前窗口显示名】（群有备注时就是备注本身）、remark 字段一律空——
    而 audit/remark 里那套"chat_name 是真实群名"的假设从来没被实测验证过。
    要判断备注对不对，得先搞清楚真实群名到底能不能读到。"""
    wx = getattr(bot, "wx", None)
    names = [n.strip() for n in (arg or "").split("|") if n.strip()]
    if not names:
        reply(chat, "用法：看群 <群名>，多个用 | 分隔")
        return True
    out = []
    for n in names:
        with MAIN_WINDOW_LOCK:
            switched = _switched(wx, n, exact=False)
            info = None
            if switched:
                try:
                    info = wx.ChatInfo()
                except Exception as e:
                    info = f"ChatInfo 异常：{e}"
        out.append(f"「{n}」切群={switched}\n  {info!r}")
        time.sleep(0.4)
    reply(chat, "\n".join(out))
    return True


def _inspect_sessions(bot, chat, arg) -> bool:
    """「看会话 N」：把 GetSession() 前 N 个 SessionElement 的属性全打出来。只读。
    看看会话列表元素里有没有比 GetAllRecentGroups 更完整的信息
    （后者的显示名会被截断到 16 字左右，长群名读回来是残的）。"""
    wx = getattr(bot, "wx", None)
    n = int(arg) if (arg or "").strip().isdigit() else 5
    try:
        with MAIN_WINDOW_LOCK:
            sessions = wx.GetSession() or []
    except Exception as e:
        reply(chat, f"GetSession 失败：{e}")
        return True
    out = [f"GetSession 返回 {len(sessions)} 项，前 {min(n, len(sessions))} 项："]
    for s in list(sessions)[:n]:
        attrs = {}
        for k in dir(s):
            if k.startswith("_"):
                continue
            try:
                v = getattr(s, k)
            except Exception:
                continue
            if not callable(v):
                attrs[k] = v
        out.append(f"  · {type(s).__name__} {attrs!r}"[:400])
    reply(chat, "\n".join(out))
    return True


def _dump_control(ctrl, depth=0, max_depth=3, out=None):
    """把一棵 uiautomation 控件树摊平成文本，只读。"""
    out = out if out is not None else []
    if ctrl is None or depth > max_depth:
        return out
    try:
        name = (ctrl.Name or "")[:40]
        out.append(f"{'  ' * depth}{ctrl.ControlTypeName} '{name}'")
        for c in ctrl.GetChildren():
            _dump_control(c, depth + 1, max_depth, out)
    except Exception as e:
        out.append(f"{'  ' * depth}<读控件失败 {e}>")
    return out


def _collect_buttons(ctrl, depth=0, max_depth=6, acc=None):
    """收集控件树里所有有名字的按钮，返回 [(名字, 控件)]。只读。"""
    acc = acc if acc is not None else []
    if ctrl is None or depth > max_depth:
        return acc
    try:
        if "Button" in ctrl.ControlTypeName and (ctrl.Name or "").strip():
            acc.append((ctrl.Name.strip(), ctrl))
        for c in ctrl.GetChildren():
            _collect_buttons(c, depth + 1, max_depth, acc)
    except Exception:
        pass
    return acc


def _inspect_panel(bot, chat, arg) -> bool:
    """「探面板 <群名>」：切到群、打开"聊天信息"面板，把里面的控件摊出来。只读。

    目的：SetGroupRemark 对已有备注是【追加】，因为它输入前不清空。要自己实现
    "清空再写"，就得先摸到备注那一栏的控件长什么样、能不能编辑。"""
    wx = getattr(bot, "wx", None)
    name = (arg or "").strip()
    lines = []
    with MAIN_WINDOW_LOCK:
        if name and not _switched(wx, name, exact=False):
            reply(chat, f"切不到「{name}」")
            return True
        try:
            lines.append(f"wx 属性：{sorted(k for k in vars(wx) if not k.startswith('__'))}")
        except Exception as e:
            lines.append(f"读 wx 属性失败：{e}")
        box = None
        for attr in ("ChatBox", "chatbox", "_chatbox", "core", "_core", "chat_box"):
            box = getattr(wx, attr, None)
            if box is not None:
                lines.append(f"chatbox 在 wx.{attr} = {type(box).__name__}")
                break
        if box is None:
            reply(chat, "\n".join(lines) + "\n没找到 chatbox，下面的探测做不了")
            return True
        # 面板得先点开，直接构造 ChatMoreInfoWnd 拿到的 control 是 None
        try:
            btns = []
            _collect_buttons(getattr(box, "control", None) or getattr(box, "root", None), 0, 6, btns)
            lines.append(f"chatbox 里的按钮：{[b[0] for b in btns][:40]}")
            for cand in ("聊天信息", "聊天详情", "更多"):
                hit = next((b for b in btns if b[0] == cand), None)
                if hit:
                    lines.append(f"点「{cand}」")
                    hit[1].Click(simulateMove=False)
                    time.sleep(1.2)
                    break
            else:
                lines.append("没找到打开面板的按钮")
        except Exception as e:
            lines.append(f"点开面板失败：{e}")
        try:
            from wxautox4.ui.component import ChatMoreInfoWnd
            wnd = ChatMoreInfoWnd(box)
            lines.append(f"ChatMoreInfoWnd 实例属性：{ {k: type(v).__name__ for k, v in vars(wnd).items()} }")
            for item in ("备注", "群聊名称", "我在本群的昵称", "群公告", "备注名"):
                try:
                    c = wnd.get_item_control(item)
                    lines.append(f"  get_item_control({item!r}) -> {c!r}")
                except Exception as e:
                    lines.append(f"  get_item_control({item!r}) 抛错：{e}")
            root = None
            for k, v in vars(wnd).items():
                if hasattr(v, "GetChildren"):
                    root = v
                    lines.append(f"面板控件树（从 wnd.{k} 起）：")
                    break
            if root is not None:
                lines.extend(_dump_control(root, 1, 3))
        except Exception as e:
            import traceback
            lines.append(f"打开面板失败：{e}\n{traceback.format_exc()}")
    reply(chat, "\n".join(lines))
    return True


def _admin_group_names(cfg) -> set:
    """不该打备注的群：管理群一旦有了备注，微信显示名（chat.who）就变成「群名🐶」，
    而管理群判定是拿 who 跟配置里的名字直接比对的——打上去等于把指令入口关掉。"""
    names = {(cfg or {}).get("admin_group"), store.DEFAULT_CONFIG.get("admin_group")}
    return {n.strip() for n in names if isinstance(n, str) and n.strip()}


def _fix_remarks(bot, chat, scope) -> bool:
    """遍历【微信里实际存在的所有群】，把备注修成「真实群名🐶」，再回写 Notion。

    跟「核对备注」的区别：那个从登记表出发（只查得到后台已知的群），这个从微信出发
    （GetAllRecentGroups），能发现后台根本没有的群——discovery 是被动的（群里有人
    说话才登记），一直沉默的群从来没进过后台。

    ★ 安全性靠"期望值就地取材"：要打的备注 = 当前窗口 ChatInfo 读到的 chat_name + 🐶，
    不是我们手上那个名字。切歪了顶多是"给另一个群打上它自己的正确备注"，不可能再
    复现 2026-08-03 那种把 A 的备注打到 B 头上的错打。

    ★ 实测前提（2026-08-03，「看群」指令打出来的）：ChatInfo 只有
    `{'chat_type','chat_name','group_member_count'}`，**没有 remark 字段**，
    `chat_name` 就是当前显示名——群有备注时它显示的是备注。所以"这个群的真实群名
    是什么"在微信侧读不到，判定只能是：显示名带🐶 = 打过了，不带 = 没打过、
    此时显示名就是真实群名。是否打对了，拿 Notion 同步下来的群名集合去核。

    改不了的只有一种：备注是追加出来的垃圾（形如「A🐶B🐶」）。SetGroupRemark 对已有
    备注是追加、空串也清不掉，硬打只会越接越长，这类只报出来让人工清。

    用法：修备注 预览（只看不改）/ 修备注 全部（真打）。"""
    wx = getattr(bot, "wx", None)
    dry = scope in ("预览", "看看", "dry")
    if not dry and scope not in ("全部", "所有", "all"):
        reply(chat, "用法：「修备注 预览」先看一遍要改什么，确认后发「修备注 全部」真打。")
        return True

    reply(chat, "开始扫描微信里的所有群（要滑一遍会话列表），稍等…")
    try:
        with MAIN_WINDOW_LOCK:
            raw = wx.GetAllRecentGroups()
    except Exception as e:
        reply(chat, f"扫描群列表失败：{e}")
        log("ERROR", f"GetAllRecentGroups 失败：{e}")
        return True

    names = audit.extract_group_names(raw)
    if not names:
        reply(chat, "没扫到任何群（GetAllRecentGroups 返回空）。先发「扫群」看看返回结构。")
        return True

    skip_names = _admin_group_names(store.load())
    names = [n for n in names if n not in skip_names and n.rstrip(audit.DOG) not in skip_names]
    known = set(registry.load().get("groups", {}))
    reply(chat, f"扫到 {len(names)} 个群（已排除管理群），开始逐个核对备注"
                f"{'（预览模式，只看不改）' if dry else ''}，大约 {max(1, len(names) * 3 // 60)} 分钟…")

    results, done_names, seen = [], [], set()
    for display in names:
        with MAIN_WINDOW_LOCK:
            info = _probe_remark(wx, display)
            if info is None:
                results.append((display, audit.FIX_SKIP, "切不过去"))
                time.sleep(0.4)
                continue
            if str(info.get("chat_type") or "") != "group":
                time.sleep(0.4)
                continue          # 同名的私聊，不是群，不管
            # 会话列表里的名字会被截断到 16 字左右，ChatInfo 读回来的才是完整显示名
            real = str(info.get("chat_name") or "").strip()
            if not real or real in seen:
                time.sleep(0.4)
                continue          # 截断名重复切到同一个群，不重复处理
            seen.add(real)
            verdict, detail = audit.plan_remark(real, known)
            if verdict == audit.FIX_APPLY and not dry:
                ok, why = _do_set_remark(wx, real, detail)
                if not ok:
                    verdict, detail = audit.FIX_FAILED, why
        results.append((real, verdict, detail))
        if verdict == audit.FIX_APPLY and real:
            done_names.append(real)                      # 这次新打的，要写进 Notion
        elif verdict == audit.FIX_OK:
            done_names.append(real[:-len(audit.DOG)])    # 已达标的，顺手确认 Notion 有🐶
        time.sleep(0.4)

    msg = audit.summarize_fix(results, dry=dry)
    if not dry and done_names:
        msg += "\n\n" + _sync_names_to_notion(done_names)
    reply(chat, msg)
    log("INFO", f"修备注完成（dry={dry}）：{len(results)} 个群")
    return True


def _do_set_remark(wx, real_name, want_remark) -> tuple[bool, str]:
    """打一个备注并回读复核。调用方须持有 MAIN_WINDOW_LOCK，且窗口已停在该群上。"""
    from . import remark as remark_mod
    ok, why = remark_mod.confirm_group_window(wx, real_name, expect_remark=want_remark)
    if not ok:
        return False, why
    try:
        r = wx.SetGroupRemark(want_remark)
    except Exception as e:
        log("ERROR", f"修备注 SetGroupRemark 抛异常 {real_name}: {e}")
        return False, str(e)
    if not remark_mod.wxresponse_ok(r):
        return False, f"SetGroupRemark 返回 {r!r}"
    time.sleep(1.0)            # 等窗口标题刷成新备注，不然回读到的还是旧显示名
    ok2, why2 = remark_mod.verify_remark(wx, real_name, want_remark)
    if not ok2:
        log("WARNING", f"修备注复核不过 {real_name}: {why2}")
        return False, f"打完复核不通过（{why2}）"
    registry.mark_remark_applied(real_name, want_remark)
    log("INFO", f"修备注：{real_name} -> {want_remark}")
    return True, want_remark


def _sync_names_to_notion(names) -> str:
    """把这批群名同步进 Notion『群聊列表』，标题统一成「群名🐶」。

    先一次性拉全表建索引再逐个比对，而不是每个群都 find_page_by_name——
    后者一个群要打 1-2 次 API，100 多个群会被 Notion 限流拖到几分钟。"""
    try:
        from . import notion_sync as ns
        rows = ns._query_all(ns.DB_GROUPS)
    except Exception as e:
        log("ERROR", f"Notion 回写跳过：{e}")
        return f"Notion 同步跳过：{e}"

    index = {}
    for row in rows:
        base, marked = ns._strip_dog(ns._title(row["properties"].get("群名")))
        if base and base not in index:
            index[base] = (row["id"], marked)

    added = fixed = already = failed = 0
    new_names = []
    for name in names:
        base, _ = ns._strip_dog(name)
        if not base:
            continue
        hit = index.get(base)
        try:
            if hit and hit[1]:
                already += 1
            elif hit:
                ns.update_title_dog(hit[0], base)
                fixed += 1
            else:
                ns.push_discovery(base, with_dog=True)
                added += 1
                new_names.append(base)
        except Exception as e:
            failed += 1
            log("WARNING", f"回写 Notion 失败 {base}: {e}")

    out = [f"Notion『群聊列表』已更新：新增 {added} 行、补🐶 {fixed} 行、本来就对 {already} 行"
           + (f"、失败 {failed} 行（见日志）" if failed else "")]
    if new_names:
        out.append("新增的群请去 Notion 里选分组、勾允许转发：")
        out.extend(f"  - {n}" for n in new_names)
    return "\n".join(out)


# ------------------------------------------------------------------ 拉群 / 迎新（config.json）

def _format_invites(cfg) -> str:
    notion_kw = registry.load().get("invite_keywords", {})
    manual_kw = cfg.get("invite", {}).get("keywords", {})
    if not notion_kw and not manual_kw:
        return ("还没有拉群关键词。去 Notion『迎新拉群』表添加后发「同步」，"
                "或用「设拉群 <关键词>|<目标群>」本地添加。")
    lines = ["拉群关键词（用户私聊我或在群里发关键词即可被拉群）："]
    if notion_kw:
        lines.append(f"— Notion『迎新拉群』表（{len(notion_kw)} 条）—")
        lines.extend(f"◾ {k} → {v}" + ("　⚠️被本地覆盖" if k in manual_kw else "")
                     for k, v in notion_kw.items())
    if manual_kw:
        lines.append(f"— 本地（设拉群，{len(manual_kw)} 条，同名时优先）—")
        lines.extend(f"◾ {k} → {v}" for k, v in manual_kw.items())
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
