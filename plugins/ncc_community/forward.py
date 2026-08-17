# -*- coding: utf-8 -*-
"""管理群转发与管理指令 —— 引擎的②。

交互习惯对齐旧 WCRobot（ncc/ncc_manager.py）：发「ncc」弹主菜单，数字选择。

  ncc              -> 主菜单：1转发 / 2面板地址 / 3待归类 / 4迎新拉群 / 0退出
  1                -> 进入转发：把消息【一条条发完】
  1                -> 收集完，进入选分组
  2+4+6            -> 多选分组编号（1=所有群聊），后台群发
  0                -> 随时退出

投递沿用旧模型（照搬 ncc_manager 的 _process_forward_queue）：
- 一群一群发（`msg.forward(单个群)`），绕开微信"多选转发≤9"上限；
- 防风控延迟：群间 3-5s、每 10 群额外 5-10s、消息间 1-2s、重试 3 次；
- 后台线程跑，收集/接收不阻塞。
- 视频号是 type='other'，会正常收集与转发；真转不了的连续 2 群失败即跳过并汇报。

分组/权限来自本地 registry.json，由面板 `/ncc_community` 维护（2026-08-05 去
Notion 化，见 PANEL_SPEC.md）——**「同步」指令已下线**，改配置不再需要拉取。
状态按发送人隔离。
"""
from __future__ import annotations

import re
import time
import random
import threading
from queue import Queue

from . import store, registry, audit, panel
from .common import REPLY_PREFIX, flog, is_bot_reply, log, reply

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
from .wxlock import set_forwarding, keepalive

# 转发策略：【一个群一个群转】。多群"分别发送"多选框会把微信卡死
# （2026-07-09 实证：转 106 群时微信直接未响应）。单个转发走轻量的"发送给"对话框，稳。
DELAY = {  # 防风控/防卡死延迟（可被 config.forward.delay 覆盖）
    "group_min": 2.5, "group_max": 4.5,     # 每个群之间
    "msg_min": 5.0, "msg_max": 8.0,         # 每条消息之间
    "batch_every": 10, "batch_min": 5.0, "batch_max": 9.0,   # 每 N 群额外歇一会儿
    "max_retries": 2,
    # 单次 forward 调用的硬超时（秒）。wxautox 自己那个 timeout 只管对话框弹出，
    # 搜不到目标时它无限等且攥着全局 uilock，必须我们兜（见 _forward_call）。
    # 12 秒是权衡：搜得到的群一两秒就返回，搜不到的等再久也是白等，而每个群最多试
    # 2 个候选串 —— 定成 25 秒的话，105 个群全搜不到要干等 87 分钟。
    "call_timeout": 12.0,
    "locate_timeout": 60.0,   # 定位要滚 120 条历史，给足，但不能没有上限
    # 连续多少个群一个都搜不到就中止整轮：寻址串要是普遍不对（比如登记表里全是微信里
    # 不存在的备注），越往下跑越白跑，早点收手叫人来查
    "give_up_after_misses": 3,
    # ★ 搜不到时用【同一个串】再试几次（2026-08-15 加）。实测微信搜索有 ~25% 的抖动：
    # 「数游大会交流群3️⃣🐶」8 轮里有 2 轮返回 0 项/11 项且未命中，耗时都 >1s（正常
    # 520~640ms），也就是读到了结果还没收敛的中间态。在此之前搜不到就直接判这个群
    # 失败、零重试，抖一下就漏一个群。代价：真的没了的群要多等 2×call_timeout。
    "miss_retries": 2,
    "miss_gap": 1.5,
}

# 收集时忽略的消息类型：只跳真噪音。视频号是 'other'，必须收集。
_SKIP_COLLECT_TYPES = {"time", "system"}

MAIN_MENU = (
    "🐶 NCC 社群管理（本群成员皆管理员）\n"
    "请回复数字：\n"
    "1 👈 转发消息\n"
    "2 👈 管理面板地址（分组/权限/拉群关键词都在那儿改）\n"
    "3 👈 待归类新群\n"
    "4 👈 迎新 / 拉群设置\n"
    "0 👈 退出管理模式\n"
    # ★ 体检指令已搬到面板「体检」页签（2026-08-15）。
    # 原来这里列着一串要手打的中文指令，问题有三：菜单状态会把文本吃掉（发了回你
    # "请输入有效的选项"）、指令串错一个字就静默不认、跑起来几分钟没有任何进度。
    # 面板上是按钮 + 实时结果 + 危险操作二次确认，没有一样是聊天框给得了的。
    "\n体检（检查群组 / 核对备注 / 扫群 / 查新群 / 修备注）已挪到面板，\n"
    "回复 2 拿地址，进去点「体检」页签，有按钮和实时进度。\n"
    "老指令仍然认，手打也能用。"
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


TIMEOUT_MARK = "转发对话框超时"     # 超时错误的识别串（判定"这个串搜不到"用）
STUCK_MARK = "转发线程卡死未释放"    # ESC 也救不回来 —— 整轮必须中止
STUCK_GRACE = 5.0                  # 关掉对话框后，再给卡住的调用多少秒退出（测试里调小）


def _wx_windows() -> set:
    """微信进程当前的可见顶层窗口句柄集合。非 Windows / 缺依赖返回空集。"""
    out = set()
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return out

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                return
            out.add(hwnd)
        except Exception:
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return out


def _dismiss_windows(hwnds) -> int:
    """给这些窗口发 ESC / 关闭消息，把卡住的"发送给"对话框弄走。

    ⚠️ 不能用 wxautox 自己的 `close()` —— 它带 @uilock，而此刻那把锁正被卡死的
    forward() 攥着，调它只会连自己一起挂上去。这里直接走 win32 消息，绕开 wxautox。"""
    n = 0
    try:
        import win32api
        import win32con
        import win32gui
    except Exception:
        return 0
    for h in hwnds:
        try:
            win32gui.PostMessage(h, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
            win32gui.PostMessage(h, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
            n += 1
        except Exception:
            continue
    time.sleep(0.5)
    for h in hwnds:                     # ESC 不认账的，再补一刀关闭
        try:
            if win32gui.IsWindow(h) and win32gui.IsWindowVisible(h):
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
        except Exception:
            continue
    _ = win32api
    return n


def _ui_call(fn, timeout: float, label: str):
    """限时跑一个 wxautox UI 调用。返回 (完成了吗, 返回值, 异常)。

    为什么每一步都要限时（2026-08-04 第二次卡死）：上一版只给 forward() 加了超时，
    结果两次卡死都停在 `_locate` 里 —— GetAllMessage / roll_into_view 同样会无限等，
    照样攥着 wxautox 的全局 uilock 把整个进程拖死，日志停在「回调触发停止」一动不动。
    凡是进 wxautox 的调用都得有闸。超时后线程还活着（Python 杀不掉），uilock 不会还
    回来，所以调用方拿到 done=False 就该按"这条线已经废了"处理，别接着往下试。"""
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:                      # noqa: BLE001 —— 原样带回
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name=f"ncc-ui-{label}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        log("ERROR", f"UI 调用「{label}」超过 {timeout}s 未返回（wxautox 全局锁多半已经废了）")
        return False, None, None
    return True, box.get("r"), box.get("e")


def _forward_call(element, group, timeout: float) -> tuple[bool, str]:
    """调一次 `element.forward(group)`，但【限时】。返回 (成功, 错误)。

    为什么必须自己限时（2026-08-04 事故）：wxautox 的 `forward(targets, timeout=3)`
    里那个 timeout 只管"等发送给对话框弹出来"，`SelectContactWnd.search()/send()`
    压根没有超时参数 —— 目标搜不到时它在里面【无限等】，而且整个 forward 带
    @uilock，于是这一个卡住的线程攥着全局 UI 锁，进程里所有微信操作全停。
    ui_watchdog 还抓不到（主循环心跳照常打），实测干等了 12 分钟一动不动。

    超时后的动作：找出调用期间新冒出来的微信窗口（就是那个"发送给"对话框），
    直接发 ESC 把它关掉，逼卡在里面的 send() 退出、释放 uilock。认新窗口而不是
    按类名找，是 ai_news_note 3.11 那次残留编辑器自锁教的。"""
    before = _wx_windows()
    box = {}

    def run():
        try:
            box["r"] = element.forward(group)
        except Exception as e:            # noqa: BLE001 —— 原样带回给调用方判定
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name="ncc-forward-call")
    t.start()
    t.join(timeout)

    if not t.is_alive():
        if "e" in box:
            raise box["e"]
        r = box.get("r")
        return (True, "") if (r is None or r) else (False, _wxresponse_message(r) or "无结果")

    # 卡住了：关掉新冒出来的对话框，给它 5 秒退出
    leftover = _wx_windows() - before
    closed = _dismiss_windows(leftover)
    log("WARNING", f"「{group}」转发调用超过 {timeout}s 未返回，已关闭 {closed} 个残留窗口")
    t.join(STUCK_GRACE)
    if t.is_alive():
        return False, f"{STUCK_MARK}（「{group}」，ESC 也没能让它退出）"
    return False, f"{TIMEOUT_MARK}：「{group}」在发送给对话框里搜不到"


def _forward_one_shot(cache_box, bot, source, sig, occ, spec, d) -> tuple[bool, str, str]:
    """把定位到的消息转发给【单个群】（走轻量的"发送给"对话框，不碰会卡死微信的
    "分别发送"多选框）。spec 是 {"name","cands"}：候选串依次试，命中即用。
    stale 才重定位重试；搜不到就换下一个候选，都不行才算这个群失败。
    返回 (成功, 错误, 命中的寻址串)。"""
    last = ""
    cands = spec.get("cands") or [spec.get("name")]
    timeout = float(d.get("call_timeout", 12))
    locate_timeout = float(d.get("locate_timeout", 60))
    miss_retries = int(d.get("miss_retries", 2))      # 搜不到时，同一个串再试几次
    miss_gap = float(d.get("miss_gap", 1.5))          # 两次之间等多久（让搜索结果收敛）
    for _ in range(int(d["max_retries"])):
        with MAIN_WINDOW_LOCK:
            if cache_box[0] is None:
                # 定位要滚 120 条历史，慢是正常的，但不能没有上限 —— 8/4 两次卡死都卡在这儿
                done, el, err = _ui_call(lambda: _locate(bot, source, sig, occ),
                                         locate_timeout, "locate")
                if not done:
                    return False, f"{STUCK_MARK}（定位消息时卡死）", ""
                if err is not None:
                    return False, str(err), ""
                cache_box[0] = el
            if cache_box[0] is None:
                last = "视图中定位不到该消息"
            else:
                for cand in cands:
                    try:
                        # ★★ 同一个串要重试（2026-08-15 实测加的）：
                        # 「数游大会交流群3️⃣🐶」连打 8 轮主窗口搜索，5 轮正常返回 2 项，
                        # 第 6 轮返回 11 项且未命中、第 7 轮返回 0 项 —— 出错的两轮耗时
                        # 都 >1000ms，正常轮次 520~640ms。也就是说微信搜索在慢路径上
                        # 会吐【中间态】（结果正在展开 / 正被清空重建），25% 的抖动率。
                        # 而这里原来是"搜不到就换下一个候选串"，候选只有一个实测串时
                        # 等于【零重试】—— 抖一下就漏一个群，86 个群的群发会稳定漏掉
                        # 一批，日志还只报 MISS，看着像"这个群没了"。
                        # 只对"搜不到"重试：卡死要立刻收手，其它错重试也没意义。
                        for att in range(miss_retries + 1):
                            done, _, _ = _ui_call(cache_box[0].roll_into_view, 15, "roll")
                            if not done:
                                return False, f"{STUCK_MARK}（滚动到消息时卡死）", ""
                            ok, err = _forward_call(cache_box[0], cand, timeout)
                            if ok:
                                if att:
                                    flog(f"「{spec['name']}」第 {att + 1} 次尝试才命中"
                                         f"（前 {att} 次搜不到，多半是搜索抖动）")
                                return True, "", cand
                            last = err
                            if STUCK_MARK in err:
                                return False, err, ""     # 锁没救回来，别再试了
                            if not (TIMEOUT_MARK in err or _is_gone(err)):
                                break                     # 不是"搜不到"，重试无意义
                            if att < miss_retries:
                                flog(f"「{spec['name']}」搜不到（第 {att + 1} 次），"
                                     f"等 {miss_gap}s 用同一个串重试", "WARNING")
                                time.sleep(miss_gap)
                        continue                      # 这个候选彻底不行 → 换下一个
                    except Exception as e:
                        last = str(e)
                        if _is_stale(last):
                            cache_box[0] = None       # 元素失效 → 跳出去重定位重试
                            break
                        return False, last, ""        # 其它错误 → 不重试
                else:
                    return False, last, ""            # 候选都试过了，这个群确实找不到
        time.sleep(1.5)
    return False, last, ""


def _classify(err: str) -> str:
    """把失败归成可 grep 的固定标签。事后统计"到底是哪类挂的"全靠它。"""
    if STUCK_MARK in err:
        return "STUCK"          # 全局 UI 锁没救回来，整轮完蛋
    if "定位不到该消息" in err:
        return "NOLOC"          # 源消息没定位到，不是群的问题
    if TIMEOUT_MARK in err or _is_gone(err):
        return "MISS"           # "发送给"对话框里搜不到这个群
    return "ERR"                # 说不清的，人来看


def _forward_located_message(bot, source, sig, occ, targets, d, msg_no=1, msg_total=1):
    """把一条消息（按签名+序号定位）【一个群一个群】转发。无结果的群记下来，其余照发。
    每 batch_every 个群额外歇一会儿。返回 (成功群数, 无结果群列表, 其它失败[str])。
    targets 是 _normalize_targets 出来的 spec 列表。"""
    cache_box = [None]
    ok = 0
    gone = []
    failed = []
    sent = []          # 成功收到本条的群（人要的"到底发给了谁"）
    stuck = False
    miss_streak = 0
    give_up_after = int(d.get("give_up_after_misses", 3))
    for i, spec in enumerate(targets):
        g = spec["name"]
        if i > 0 and i % int(d["batch_every"]) == 0:
            time.sleep(random.uniform(d["batch_min"], d["batch_max"]))
        t_g = time.time()
        success, err, hit = _forward_one_shot(cache_box, bot, source, sig, occ, spec, d)
        cost = time.time() - t_g
        keepalive()          # 转发仍在推进，给主窗口闸门续期（别让它半路失效被人抢窗口）
        # ★ 每个群一行，成功也记（2026-08-15）。原来只有异常路径写日志，于是一次
        # 86 个群的群发在日志里近乎空白，卡住时连"跑到第几个"都答不上来。
        _pos = f"[群发 {i + 1}/{len(targets)} 第{msg_no}/{msg_total}条]"
        if success:
            flog(f"{_pos} 「{g}」 ✅ OK 串「{hit}」 {cost:.1f}s")
        else:
            flog(f"{_pos} 「{g}」 ❌ {_classify(err)} 候选{spec.get('cands')} "
                 f"{cost:.1f}s —— {err}", "WARNING")
        if success:
            ok += 1
            sent.append(g)
            miss_streak = 0                         # 有一个成功就说明寻址整体没坏
            if hit and hit != (spec.get("cands") or [None])[0]:
                # 首选串没搜到、备选串命中 → 登记表里的寻址状态是错的，就地纠正
                log("INFO", f"「{g}」用备选串「{hit}」命中，纠正登记表寻址")
                try:
                    registry.mark_addressing(g, hit)
                except Exception as e:
                    log("WARNING", f"回写寻址状态失败 {g}: {e}")
        elif STUCK_MARK in err:
            failed.append(f"{g}: {err}")
            stuck = True
            log("ERROR", f"UI 锁没能释放，中止本条转发：{err}")
            break                                   # 再转下去只会一路卡死
        elif "定位不到该消息" in err:
            failed.append(f"{g}: {err}")            # 源消息定位不到，非群的问题
        elif TIMEOUT_MARK in err or _is_gone(err):
            gone.append(g)                          # 候选串都搜不到 → 该群不可达
            miss_streak += 1
            if give_up_after and miss_streak >= give_up_after:
                # 寻址普遍失效时（登记表里的名字微信里都不存在），越往下跑越白跑：
                # 每个群白等 2×call_timeout，105 个群能干耗掉半小时以上。早点收手叫人。
                failed.append(f"连续 {miss_streak} 个群都搜不到，已中止本条（寻址串多半普遍不对）")
                log("ERROR", f"连续 {miss_streak} 个群搜不到，中止本条转发")
                break
        else:
            # 超时、UI 异常这类说不清的错，只汇报不下结论：gone 会被 mark_unreachable
            # 写进 registry（allow_forward=False），之后所有转发都跳过这个群，
            # 没人去面板核对就是永久漏发。宁可少标一个，别冤枉一个。
            failed.append(f"{g}: {err}")
        time.sleep(random.uniform(d["group_min"], d["group_max"]))

    # 保护：整条一个群都没成功 → 判定是这条消息本身转不了，别冤枉群（不标记任何群不可达）
    if ok == 0 and (gone or failed):
        if stuck:
            return 0, [], failed, []   # 卡死的真相要原样带出去，别被"消息不支持转发"盖掉
        return 0, [], ["整条转发失败（该消息可能不支持转发，未标记任何群）"], []
    return ok, gone, failed, sent


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


def _normalize_targets(targets):
    """把目标清单统一成 [{"name","cands"}]。

    兼容两种形态：registry.forward_specs 出来的 dict（带寻址候选），以及历史/测试里
    的纯字符串（只有一个候选，行为与以前一致）。

    ⚠️ dict 形态下 cands 为空【不能】回落成群名 —— 空是 forward_specs 把危险候选
    摘光的结果（见 registry.unsafe_candidate），回落等于把刚摘掉的串又捡回来。
    空 cands 的 spec 由 _split_addressable 挑出去汇报，不进投递。"""
    out = []
    for t in targets or []:
        if isinstance(t, dict):
            cands = [str(c) for c in (t.get("cands") or []) if c]
            name = t.get("name") or (cands[0] if cands else "")
            out.append({"name": str(name), "cands": cands,
                        "blocked": list(t.get("blocked") or [])})
        else:
            out.append({"name": str(t), "cands": [str(t)], "blocked": []})
    return out


def _split_addressable(specs):
    """把目标清单拆成 (能安全寻址的, 寻址串会误伤别人的)。后者必须汇报给人，
    不能静默跳过 —— 静默跳过就是"某个群从此再也收不到"，而没人会发现。"""
    ok, unsafe = [], []
    for s in specs:
        (ok if s.get("cands") else unsafe).append(s)
    return ok, unsafe


def _unsafe_lines(unsafe):
    """把"寻址串会误伤别人"的群渲染成人能直接照着处理的一段话。"""
    if not unsafe:
        return []
    rows = []
    for s in unsafe[:10]:
        why = "、".join(f"「{c}」会同时命中「{v}」" for c, v in (s.get("blocked") or [])[:2])
        rows.append(f" - {s['name']}：{why or '没有安全的寻址串'}")
    return [f"⚠️ {len(unsafe)} 个群【没发】：寻址串是别的群名字的一部分，微信搜索是包含匹配，"
            f"发出去就可能进错群，所以宁可不发：\n" + "\n".join(rows)
            + (f"\n…另有 {len(unsafe) - 10} 个" if len(unsafe) > 10 else "")
            + "\n👉 去面板给这些群点「改名」对齐微信里的真名，或发「恢复群 <群名>」清掉旧寻址串重学。"]


def _deliver(task) -> dict:
    """先拿本次要转消息的【签名清单】(身份)，再【逐条】：滚动定位到该消息、取当下
    有效元素、转发给各批群。每条只在"可见"时才转，规避元素失效。按时序逐条处理。
    主窗口锁已和主循环共用，转发期间主循环不会来抢窗口。
    """
    bot = task["bot"]; admin = task["admin"]
    targets, unsafe = _split_addressable(_normalize_targets(task["targets"]))
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
    sent_all = set()          # 至少成功收到过一条的群（去重）
    fail_detail = []

    aborted = False
    t_run = time.time()
    flog(f"===== 群发开始 {label} 目标 {len(targets)} 群 × {n_msgs} 条 "
         f"operator={operator} 跳过(寻址有歧义) {len(unsafe)} =====")
    for spec in unsafe:
        flog(f"  ⚠️ 不发「{spec['name']}」：{spec.get('blocked')}", "WARNING")
    for mi, (sig, occ) in enumerate(sigs):
        flog(f"--- 第 {mi + 1}/{n_msgs} 条：{sig} occ={occ} ---")
        okc, gone, failed, sent = _forward_located_message(bot, admin, sig, occ, targets, d,
                                                           msg_no=mi + 1, msg_total=n_msgs)
        ok += okc
        fail += len(gone) + len(failed)
        gone_all.update(gone)
        sent_all.update(sent)
        if failed:
            fail_detail.extend(f"第{mi+1}条 {x}" for x in failed[:5])
        if any(STUCK_MARK in x for x in failed):
            # 全局 UI 锁没释放，后面的消息只会一路卡死 —— 整轮收手，交给人处置
            aborted = True
            log("ERROR", "转发中止：微信 UI 锁未释放，请重启程序后再试")
            break
        time.sleep(random.uniform(d["msg_min"], d["msg_max"]))

    # ★ 搜不到的群【只报告，不自动禁】（2026-08-13 定，人的决定）。
    # 原来是一失败就 mark_unreachable（allow_forward=False），省事但会误伤：
    # 微信搜索被实测证明会抖 —— 同一个群这一轮搜到、下一轮搜不到（「爱和未来」
    # 「全国旅居2群」都这样过）。一次抖动就把一个好群永久禁掉，而且没人会立刻发现，
    # 只会某天觉得"这个群怎么没收到"。群列表要精准，就不能让机器悄悄改它。
    # 现在把清单交给人，确认后用「禁群 <群名>」或面板处理。
    marked = sorted(gone_all)

    # 收尾：主窗口回到最新
    try:
        with MAIN_WINDOW_LOCK:
            bot.wx.ChatWith(admin, exact=False)
    except Exception:
        pass

    missed = [t["name"] for t in targets if t["name"] not in sent_all]
    flog(f"===== 群发{'中止' if aborted else '结束'} {label} "
         f"成功 {ok} 条次 / 失败 {fail} 条次 | 收到的群 {len(sent_all)} / 没收到 {len(missed)} "
         f"| 耗时 {time.time() - t_run:.1f}s =====")
    if missed:
        flog("  没收到的群：" + "、".join(missed), "WARNING")
    lines = [f"{REPLY_PREFIX} {'转发中止！' if aborted else '转发完成！'}{label}",
             f"成功 {ok} 条次，失败 {fail} 条次，目标 {len(targets)} 个群 × {n_msgs} 条。",
             f"📊 收到的群 {len(sent_all)} 个，没收到的 {len(missed)} 个。"]
    lines.extend(_unsafe_lines(unsafe))
    if missed:
        # 人要的是"到底哪几个群没发到"——只给数字，事后还得自己一个个去翻
        lines.append("❌ 没收到的群：\n" + "\n".join(f" - {m}" for m in missed[:20])
                     + (f"\n…另有 {len(missed) - 20} 个" if len(missed) > 20 else ""))
    if aborted:
        lines.append("⛔ 微信 UI 锁没能释放，剩下的没转。请重启程序（SWXPanelRestart）后重来。")
    if marked:
        lines.append(f"🚫 {len(marked)} 个群没搜到（**没有自动禁**，微信搜索会抖，可能只是这次没搜着）：\n"
                     + "\n".join(f" - {m}" for m in marked[:15])
                     + (f"\n…另有 {len(marked) - 15} 个" if len(marked) > 15 else "")
                     + "\n👉 在微信里确认一下：确实没了就发「禁群 <群名>」移出列表，还在就重发一次。")
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
    if text == "2":       # 管理面板
        reply(chat, _backend_hint())
        return True
    if text == "3":       # 待归类新群
        reply(chat, _format_pending())
        return True
    if text in ("4", "5"):   # 迎新 / 拉群设置（5 是老编号，手指记着呢，留个兼容）
        reply(chat, _format_welcome_invite(cfg))
        return True
    # ★ 主菜单状态下也要认直接指令（2026-08-15 修）。
    # 之前：只要处在菜单状态，任何非 1/2/3/4/0 的文本都被这里的兜底吃掉，
    # 于是人照着菜单上"体检指令（直接发）"那几行发「检查群组 全部」，
    # 回的却是"请输入有效的选项"+ 同一份菜单 —— 菜单教你发，菜单又不让你发，
    # 死循环，看着像"机器人读不懂人话"。（截图实测：连发两次，两次都被顶回来。）
    if _try_direct_command(bot, chat, cfg, sender, text):
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
        lines.append("（暂无分组，去面板「分组管理」建一个：" + panel.panel_url() + "）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 转发：选分组 + 入队

def _handle_choose(bot, chat, cfg, sender, st, text) -> bool:
    if not re.fullmatch(r"\s*\d+(\s*\+\s*\d+)*\s*", text or ""):
        reply(chat, "请输入有效的选项（支持多选，如：2+4+6），或发送【0】退出转发模式")
        return True
    nums = [int(x) for x in re.split(r"\s*\+\s*", text.strip())]
    data = registry.load()

    # 用 forward_specs 而不是单一寻址串：每个群带上候选（群名/备注），
    # 转发时依次试，避免一个错串（比如微信里根本没打上的🐶备注）把整轮卡死。
    targets, label = [], ""
    if 1 in nums:  # 所有群聊
        targets = registry.forward_specs(data)
        label = "分组「所有群聊」"
    else:
        chosen, seen = [], set()
        for num in nums:
            gname = registry.grouping_name_by_number(data, num)
            if not gname:
                continue
            chosen.append(gname)
            for spec in registry.forward_specs(data, gname):
                if spec["name"] not in seen:
                    seen.add(spec["name"])
                    targets.append(spec)
        if not chosen:
            reply(chat, "这些编号都没对应到分组，请重新选择，或发送【0】退出")
            return True
        label = "分组「" + "、".join(chosen) + "」"

    if not targets:
        reply(chat, "未找到任何可转发的群组（去面板看看这些群勾没勾「允许转发」），或发送【0】退出")
        return True

    # 寻址串会误伤别的群的，这里就别算进"要发几个群"了 —— 报个准数，人才好核对。
    # 它们仍旧塞进 targets 交给 worker，由最终汇报统一列出来（一份报告，别报两遍）。
    safe, unsafe = _split_addressable(_normalize_targets(targets))
    if not safe:
        reply(chat, "\n".join(["这一批群一个都发不了："] + _unsafe_lines(unsafe)))
        return True

    count = st.get("count", 0)
    _clear_state(sender)
    # 不传消息对象：worker 投递前从源群现读 operator 的内容（新鲜元素、不漏不失效）
    _QUEUE.put({"bot": bot, "admin": cfg.get("admin_group"), "operator": sender,
                "targets": list(targets), "label": label, "delay": _delays(cfg)})
    reply(chat, f"开始转发 {count} 条消息到 {len(safe)} 个群…"
                + (f"（另有 {len(unsafe)} 个群寻址串有歧义，不发，完成后一并汇报）" if unsafe else "")
                + "\n为避免风控，将会添加随机延迟，请耐心等待，完成后我在这儿汇报。")
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
    if plain in ("后台", "面板", "管理面板"):
        reply(chat, _backend_hint()); return True
    if plain in ("分组列表", "转发分组"):
        reply(chat, _format_groupings()); return True
    if plain in ("待归类", "新群"):
        reply(chat, _format_pending()); return True
    if plain == "拉群列表":
        reply(chat, _format_invites(cfg)); return True
    if plain == "迎新列表":
        reply(chat, _format_welcomes(cfg)); return True

    if plain in ("查新群", "新群检查", "没打标签"):
        return _find_unmarked(bot, chat)
    if plain in ("扫群", "扫描群列表"):
        return _scan_groups(bot, chat)

    m = re.match(r"^(检查群组|核对备注|修备注|看群|看会话|试改备注|探搜索|测搜索|探备注面板|查寻址|禁群|恢复群|开迎新|关迎新|删拉群)\s*(.+)$", text, re.S)
    if m:
        name = m.group(2).strip()
        return {
            "探搜索": lambda: _probe_search(bot, chat, name),
            "测搜索": lambda: _bench_search(bot, chat, name),
            "探备注面板": lambda: _probe_remark_panel(bot, chat, name),
            "查寻址": lambda: _fix_addressing(bot, chat, name),
            "禁群": lambda: _disable_group(chat, name),
            "恢复群": lambda: _enable_group(chat, name),
            "检查群组": lambda: _check_groups(bot, chat, name),
            "核对备注": lambda: _audit_remarks(bot, chat, name),
            "修备注": lambda: _fix_remarks(bot, chat, name),
            "看群": lambda: _inspect_chat(bot, chat, name),
            "看会话": lambda: _inspect_sessions(bot, chat, name),
            "试改备注": lambda: _try_edit_remark(bot, chat, name),
            "开迎新": lambda: _toggle_welcome(chat, cfg, name, True),
            "关迎新": lambda: _toggle_welcome(chat, cfg, name, False),
            "删拉群": lambda: _delete_invite(chat, cfg, name),
        }[m.group(1)]()

    m = re.match(r"^(设拉群|设迎新文案|设迎新链接|设备注)\s*(.+?)\s*\|\s*(.*)$", text, re.S)
    if m:
        cmd, a, b = m.group(1), m.group(2).strip(), m.group(3).strip()
        return {
            "设拉群": lambda: _set_invite(chat, cfg, a, b),
            "设备注": lambda: _set_remark_override(chat, cfg, a, b),
            "设迎新文案": lambda: _set_welcome_field(chat, cfg, a, "text", b),
            "设迎新链接": lambda: _set_welcome_field(chat, cfg, a, "url", b),
        }[cmd]()
    return False


# ------------------------------------------------------------------ 同步 / 查看

def _backend_hint() -> str:
    return ("管理面板（分组 / 转发权限 / 迎新链接 / 拉群关键词 / 新群归类都在这儿改，"
            "改完下一条消息即生效）：\n" + panel.panel_url())


def _do_sync(chat) -> bool:
    """「同步」已下线（2026-08-05 去 Notion 化）。

    保留这个入口只为回一句人话——直接不认识这条指令的话，习惯性发「同步」的人
    会以为机器人挂了。"""
    reply(chat, "「同步」已经下线啦：分组/权限/拉群关键词现在直接在面板上改，"
                "改完立刻生效，不用再拉取。\n" + _backend_hint())
    return True


def _format_groupings() -> str:
    data = registry.load()
    groupings = registry.forward_groupings_detailed(data)
    if not groupings:
        return "还没有可转发的分组。去面板「分组管理」建一个：\n" + panel.panel_url()
    lines = [f"转发分组（共 {len(groupings)} 个，编号即选择号）："]
    for name, num, cnt in groupings:
        lines.append(f"  {num} 👈 {name}（{cnt} 群）")
    lines.append(f"最后改动：{data.get('synced_at') or '未记录'}")
    return "\n".join(lines)


def _format_pending() -> str:
    data = registry.load()
    pend = [name for name, g in data.get("groups", {}).items() if g.get("status") == "pending"]
    if not pend:
        return "没有待归类的新群。"
    lines = [f"待归类新群（{len(pend)} 个，去面板「待归类」选分组+勾允许转发）："]
    lines.extend(f"  - {n}" for n in pend)
    lines.append(panel.panel_url())
    return "\n".join(lines)


def _format_welcome_invite(cfg) -> str:
    return ("迎新 / 拉群设置：\n"
            "【迎新】面板上给群填「迎新链接」即开启该群迎新卡片。\n"
            "  文案：设迎新文案 <群名>|<文案>（{name}=新人昵称）\n"
            "  开关：开迎新 <群名> / 关迎新 <群名>；查看：迎新列表\n"
            "【拉群】关键词在面板「拉群关键词」页增删改；查看：拉群列表\n"
            "  应急也可在群里发：设拉群 <关键词>|<目标群>；删拉群 <关键词>\n"
            "面板：" + panel.panel_url() + "\n"
            "回复 0 退出管理模式。")


SEARCH_EMPTY_RETRIES = 2   # 主窗口搜索返回 0 项时，重搜几次（微信会吐中间态）
SEARCH_RETRY_GAP = 0.8     # 两次重搜之间等多久
CHATINFO_SETTLE = 0.6      # ChatWith 之后等窗口结算的时间（秒）
CHATINFO_TRIES = 3         # 读回的名字对不上时最多重读几次


def _read_chat_name(wx, want: str) -> str:
    """（须在 MAIN_WINDOW_LOCK 内调用）ChatWith 之后读【当前会话显示名】。

    ★★ 为什么不能读一次就信（2026-08-14 事故根因）：
    `ChatWith` 返回后窗口并没有立刻结算，紧跟着调 `ChatInfo()` 会读回【上一个会话】
    的名字。8-14 那次「检查群组」87 个群里有 20 个中招，而且铁证如山 —— 报告里
    "切歪到"的那个群，7/8 正好是它在队列里的【前一个】群（序号差恒为 1），
    同一批日志里一条 `切到「X」失败` 都没有（说明 ChatWith 其实全成功了）。
    结果是 20 个好群被判成"不可达"，还建议人去面板删掉它们。

    所以：先给结算时间再读；读回的名字剥掉🐶等于目标就立刻收工，不等于就再等再读，
    重试到 CHATINFO_TRIES 次为止。这样"读慢了"只多花几百毫秒，
    而"真切歪了"才会被判失败 —— 判据不再冤枉人。
    读不到返回 ""（调用方退回用"切成功的那个串"）。"""
    seen = ""
    for i in range(CHATINFO_TRIES):
        time.sleep(CHATINFO_SETTLE)
        try:
            info = wx.ChatInfo() or {}
        except Exception:
            info = {}
        seen = str(info.get("chat_name") or "").strip()
        if seen and audit.strip_dog(seen) == want:
            return seen                      # 对上了就不用再等
        if i == 0 and seen:
            log("DEBUG", f"读回「{seen}」≠ 目标「{want}」，等窗口结算后重读")
    return seen                              # 重试完还不对：这才算真的切歪了


def _check_groups(bot, chat, name) -> bool:
    data = registry.load()
    if name in ("全部", "所有", "all"):
        specs = registry.forward_specs(data)
    elif name in data.get("groupings", {}):
        specs = registry.forward_specs(data, name)
    else:
        reply(chat, f"没有「{name}」这个分组。发「分组列表」看看。")
        return True
    if not specs:
        reply(chat, "该分组没有允许转发的群可检查。")
        return True
    specs, unsafe = _split_addressable(_normalize_targets(specs))
    if not specs:
        reply(chat, "\n".join(["这一批群一个都查不了："] + _unsafe_lines(unsafe)))
        return True
    reply(chat, f"开始检查 {len(specs)} 个群，顺便学习每个群在微信里的真实显示名，请稍等…")
    ok_list, fail_list, fixed, odd = [], [], [], []
    wx = getattr(bot, "wx", None)
    for spec in specs:
        keepalive()   # 87 个群要跑好几分钟，闸门不续期后半程会被探针抢走主窗口
        # ★ 不只判"切没切过去"，还要把【微信里的显示名】读回来存下。
        # 转发的"发送给"对话框是按显示名精确勾选的（8/10 实测：拿群名去搜一个已打🐶的群，
        # 搜得到但勾不中，send() 在里面死等）。显示名才是唯一该用的寻址串——
        # 与其每次转发靠 remark_applied 去猜、猜错白等一次超时，不如在这里一次性学准。
        hit, seen_name = None, ""
        for cand in spec["cands"]:
            with MAIN_WINDOW_LOCK:
                if not _switched(wx, cand, exact=False):
                    seen_name = ""
                else:
                    seen_name = _read_chat_name(wx, spec["name"])
                    if not seen_name:
                        hit = cand        # 读不到显示名，退回用"切成功的这个串"
            if hit:
                break
            if seen_name:
                # ★ 安全闸：ChatWith 是模糊匹配，完全可能切到【别的群】。显示名剥掉🐶后
                # 必须等于登记表里的群名，否则就是切歪了或群改了名 —— 绝不能把别人的名字
                # 学进来，那会让之后每一次转发都稳定地发错群，比现在的卡死更糟。
                bare = audit.strip_dog(seen_name)
                if bare == spec["name"]:
                    hit = seen_name
                    break
                # ★ 分两种情况说人话（2026-08-15）：原来一律报"切到的是「X」"，
                # 而「泰国清迈旅居交流1群」读回的是「🟪泰国清迈旅居交流1群🐶」——
                # 剥掉🐶还多个🟪前缀，于是报告写成"搜「🟪…🐶」切到的是「🟪…🐶」"，
                # 两个串肉眼一模一样，人看了只会觉得机器人疯了。
                # 真相是登记表里的群名少了前缀，该去面板改名，不是切歪了。
                if bare and (bare in spec["name"] or spec["name"] in bare):
                    odd.append(f"{spec['name']}：微信里其实叫「{bare}」——名字对不上，"
                               f"去面板点「改名」对齐")
                else:
                    odd.append(f"{spec['name']}：搜「{cand}」切到的是「{seen_name}」，"
                               f"多半是🐶备注没真打上，微信只好模糊匹配")
                log("WARNING", f"检查群组：搜「{cand}」读回的是「{seen_name}」，与目标不符")
            time.sleep(0.3)
        if hit:
            ok_list.append(spec["name"])
            if hit != spec["cands"][0]:
                fixed.append(f"{spec['name']} → 「{hit}」")
            try:
                registry.mark_addressing(spec["name"], hit)   # 学准了就记住，之后一次命中
            except Exception as e:
                log("WARNING", f"回写寻址状态失败 {spec['name']}: {e}")
        else:
            fail_list.append(spec["name"])
        time.sleep(0.5)
    lines = [f"检查完成：{len(ok_list)}/{len(specs)} 可达，已学到 {len(ok_list)} 个群的真实显示名。",
             "（显示名 = 转发时唯一能勾中的那个串，学过之后转发不用再猜、也不用白等超时）"]
    if fixed:
        lines.append(f"🔧 {len(fixed)} 个群的寻址串跟原来猜的不一样，已按实测纠正：\n"
                     + "\n".join(f" - {x}" for x in fixed[:10])
                     + (f"\n…另有 {len(fixed) - 10} 个" if len(fixed) > 10 else ""))
    if odd:
        lines.append(f"⚠️ {len(odd)} 处搜到的是别的群（模糊匹配切歪或群改了名，没敢学）：\n"
                     + "\n".join(f" - {x}" for x in odd[:8])
                     + (f"\n…另有 {len(odd) - 8} 处" if len(odd) > 8 else ""))
    lines.extend(_unsafe_lines(unsafe))
    if fail_list:
        lines.append(f"不可达 {len(fail_list)} 个（重读 {CHATINFO_TRIES} 次仍对不上，"
                     f"多半是真改了群名/退群了）：")
        lines.extend(f" - {t}" for t in fail_list)
        lines.append("👉 先在微信里搜一下确认，确实改名了就去面板点「改名」，"
                     "真退群了才删 —— 别照着这份清单直接删。")
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

    # ★ 把【全量显示名】也吐出来。只给前 5 个样本足够看清返回结构，但拿它跟登记表
    # 离线比对时必须要全量 —— 那是唯一能一刀切开"这个群真没了"和"名字挂在别的群头上"
    # 的证据，否则只能一个个切窗口去试（104 个群要 15 分钟，还会占着微信主窗口）。
    names = audit.extract_names(raw) if hasattr(audit, "extract_names") else []
    if not names:
        names = [str(r[0]) if isinstance(r, (list, tuple)) and r else str(r) for r in (raw or [])]
    reply(chat, f"耗时 {time.time() - t0:.1f} 秒。\n{desc}")
    reply(chat, "【全量显示名 %d 个】\n%s" % (len(names), "\n".join(names)))
    return True


def _norm_name(s) -> str:
    """比对用的归一化：全角标点转半角 + 去掉所有空白。

    2026-08-13 实测：登记表里是「游牧岛｜全国旅居2群」（全角竖线 U+FF5C），
    微信里显示的是「游牧岛 | 全国旅居2群🐶」（半角竖线 + 两侧空格）—— 同一个群，
    字符不同，直接比对必然对不上，于是被判成"这个群没了"。NFKC 正好把全角
    标点折叠成半角等价物，emoji 和汉字不受影响。"""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def _search_display_name(wx, group_name: str, out_detail=None):
    """用微信搜索查一个群的【实际显示名】。返回 (显示名, 诊断串) 或 (None, 原因)。

    2026-08-13 实测发现的关键事实：显示名可以带我们不知道的装饰前缀 ——
    搜「泰国清迈旅居交流1群」返回的 content 是「🟪泰国清迈旅居交流1群🐶」。
    我们拿「群名」「群名🐶」两个候选去找，对这类群一个都对不上，于是它们被
    「检查群组」判成"不可达"、被当成退群或改名，其实群好好的，只是名字前面多了个方块。

    判据：显示名必须【包含】群名，且多出来的装饰不超过 8 个字符 —— 只放行"加了点缀"，
    不放行"搜到了另一个群"。宁可漏一个也不能学错：学错等于以后每次转发都稳定发错群。"""
    sb = getattr(wx, "SessionBox", None)
    if sb is None:
        return None, "拿不到 SessionBox"

    want = _norm_name(group_name)
    best, cands, spliced, matches = None, [], [], []

    # 整名搜不到就用前缀再搜一轮：微信搜索对长串/特殊符号时灵时不灵，而判据始终是
    # "归一化后必须包含【完整】群名"，所以放宽的只是搜索词，不是匹配标准。
    # 搜索词也要给几个变体：微信搜索对全角分隔符不友好 —— 拿「游牧岛｜全国旅居2群」
    # （全角竖线）搜是 0 结果，可它明明存在，搜别的词时会以「游牧岛 | 全国旅居2群🐶」
    # 冒出来。所以除原名外，再给归一化名和"最长的一段连续文字"。
    queries = [group_name]
    normed = _norm_name(group_name)
    if normed and normed != group_name:
        queries.append(normed)
    segs = [x for x in re.split(r"[\s|｜/／\-—·、,，:：!！?？()（）\[\]【】]+", group_name) if len(x) >= 4]
    if segs:
        longest = max(segs, key=len)
        if longest not in queries:
            queries.append(longest)

    for q in queries:
        # ★ 搜之前把框清空：不清的话上一次的词会残留，于是接连几个不同的群搜出来的是
        # 【同一批结果】。2026-08-13 实测，连着 4 个群的失败详情都是
        # 「…最近在搜、旅居交流群、旅居交流群🐶」——那是上一个群留下的。
        #
        # ★★ 但这两步都必须限时。同一天的教训：这句 SendKeys 第一版没加闸，直接把
        # 整个 bot 进程卡死 38 分钟（task_result 和 wxauto 日志双双停在同一秒）。
        # "凡是进 wxautox 的调用都得有闸"这条今晚立了三次，我自己写新代码时还是漏了。
        box = getattr(sb, "searchbox", None)
        if box is not None:
            _ui_call(lambda: box.SendKeys("{Ctrl}a{Delete}", waitTime=0.15), 6, "clear-search")
            time.sleep(0.2)
        # ★ 空结果要重搜（2026-08-15 实测加）：同一个串连打 8 轮，有一轮返回 0 项、
        # 一轮返回 11 项且未命中，出错的两轮耗时都 >1s（正常 520~640ms）—— 微信搜索
        # 在慢路径上会被读到中间态（结果正被清空重建 / 还没收敛）。
        # 0 项是其中最干净、最不会误判的信号，只重试它；"非空但对不上"留给上层报人工，
        # 因为那也可能是真的改了名，无脑重试会把"群没了"拖成三倍耗时。
        res = []
        for att in range(SEARCH_EMPTY_RETRIES + 1):
            done, r, err = _ui_call(lambda: sb.search(q), 20, f"search:{q[:12]}")
            if not done:
                return None, f"{STUCK_MARK}（搜索「{q}」卡住，UI 锁多半没释放）"
            if err is not None:
                return None, f"搜索失败：{err}"
            res = r or []
            if res or att >= SEARCH_EMPTY_RETRIES:
                if res and att:
                    flog(f"搜「{q}」第 {att + 1} 次才有结果（前 {att} 次是空的，搜索抖动）")
                break
            time.sleep(SEARCH_RETRY_GAP)
        for el in res:
            c = str(getattr(el, "content", "") or "").strip()
            if not c or c in cands:
                continue
            cands.append(c)
            cn = _norm_name(c)
            # 双向匹配：① 显示名包含群名（带装饰，如 🟪xxx🐶）
            #           ② 显示名是群名的【截断】——微信群备注上限 48 字节，长群名打备注
            #              会被截掉尾巴，实测「数字游民信息共享群」在微信里就是
            #              「数字游民信息共享🐶」，少一个"群"字。差 3 字以内才认。
            bare = audit.strip_dog(cn)
            hit_fwd = want and want in cn and len(cn) - len(want) <= 8
            hit_trunc = (bare and len(bare) >= 6 and bare in want
                         and len(want) - len(bare) <= 3)
            # ★ 副标题才是身份证：备注被打错群之后，主标题显示的是【那个错备注】，
            # 微信会在下面补一行「群聊名称：真实群名」。只看主标题就会判"对不上"，
            # 而这一行直接告诉我们"这个群就是它，只是顶着别人的名字"。
            hit_sub = False
            try:
                full = _norm_name(el.get_all_text() or "")
                if want and "群聊名称" in full and want in full:
                    hit_sub = True
                    if out_detail is not None:
                        out_detail.append(f"[副标题命中] {c} ⇦ {group_name}")
            except Exception:
                pass
            if hit_fwd or hit_trunc or hit_sub:
                # ★ 不能一命中就收工：短群名用包含判据太宽松，「NCC的朋友们」同时是
                # 「NCC的朋友们16群」「…26群」的前缀，先撞上谁就学谁 = 掷骰子决定
                # 以后往哪个群发消息。所以全收集起来，多于一个就判歧义、宁可不学。
                if c not in matches:
                    matches.append(c)
                continue
            try:
                if audit.looks_spliced(c):
                    spliced.append(c)
            except Exception:
                pass
        try:                              # 收尾：关掉搜索面板，别把主窗口留在搜索页
            if res:
                res[0].close()
        except Exception:
            pass
        if matches:
            break
        time.sleep(0.3)

    if len(matches) == 1:
        best = matches[0]
    elif len(matches) > 1:
        # 多个候选时，剥掉🐶后与群名【严格相等】的那个才是本尊。
        # 实测「【大理】春节串门一起玩！（看公告」同时命中它自己的🐶备注和一个
        # 前面多了个 A 的同名群，靠这一条能自动选对，不必退回人工。
        exact = [c for c in matches if audit.strip_dog(_norm_name(c)) == want]
        # 微信里可能同时存在「旅居交流群」和「旅居交流群🐶」两个会话，剥掉🐶后都等于
        # 群名、都算 exact。带🐶的那个才是我们纳管过的，选它。
        dogged = [c for c in exact if audit.has_dog(c)]
        if len(dogged) == 1:
            best = dogged[0]
        elif len(exact) == 1:
            best = exact[0]
        else:
            return None, ("有 %d 个都像，不敢学（怕以后每次都发错群）：%s"
                          % (len(matches), "、".join(matches[:3])))
    if out_detail is not None:
        out_detail.extend(cands[:6])
    if best:
        return best, ""
    if spliced:
        return None, f"备注被追加过，需人工清空后重打：「{spliced[0]}」"
    return None, ("搜到 %d 项但都对不上：%s" % (len(cands), "、".join(cands[:4]))
                  if cands else "搜不到")


def _fix_addressing(bot, chat, scope) -> bool:
    """「查寻址 全部 / 缺失 / <群名>」：把每个群的实际显示名查出来存进 addressing_hit。

    存在的理由：转发的"发送给"对话框按显示名精确勾选，而显示名可能带 emoji 前缀、
    带🐶备注、或两者都有 —— 靠"群名/备注"两个候选去猜，对装饰过的群必然全军覆没。
    搜一次抄下来，比猜可靠得多，也比逐个 ChatWith 切窗口快。"""
    wx = getattr(bot, "wx", None)
    data = registry.load()
    if scope in ("全部", "所有", "all"):
        specs = registry.forward_specs(data)
    elif scope in ("缺失", "没学到", "missing"):
        specs = [s for s in registry.forward_specs(data)
                 if not (data["groups"].get(s["name"], {}) or {}).get("addressing_hit")]
    elif scope in data.get("groupings", {}):
        specs = registry.forward_specs(data, scope)
    else:
        specs = [s for s in registry.forward_specs(data) if s["name"] == scope]
        if not specs:
            reply(chat, f"没有「{scope}」这个群或分组。"); return True

    reply(chat, f"开始查 {len(specs)} 个群的实际显示名（微信搜索，只读不发消息）…")
    learned, changed, failed, mismatched = [], [], [], []
    for spec in specs:
        g = spec["name"]
        with MAIN_WINDOW_LOCK:
            _bring_wx_front()
            shown, why = _search_display_name(wx, g)
            if not shown:
                # ★ 微信搜索会抽风：同一个群上一轮搜得到、这一轮"搜不到"，失败详情里
                # 还常带着"最近在搜"——那是关键词压根没输进去，返回的是历史建议。
                # 更硬的反证：某些群能作为【别的词】的搜索结果冒出来（还带着🐶），
                # 按自己的名字搜却报搜不到。所以单次失败不能判"这个群没了"，重来一次。
                time.sleep(1.6)
                shown2, why2 = _search_display_name(wx, g)
                if shown2:
                    shown, why = shown2, why2
                    log("INFO", f"「{g}」第一次没搜到，重试命中")
                else:
                    why = f"{why}｜重试仍失败：{why2}"
        if shown is None and STUCK_MARK in (why or ""):
            failed.append(f"{g}：{why}")
            log("ERROR", f"搜索卡死，中止本轮：{why}")
            reply(chat, f"⛔ 搜索卡住了（UI 锁多半没释放），已中止。已处理 {len(learned)} 个群，"
                        f"请重启程序（SWXPanelRestart）后再跑一次。")
            break
        if shown:
            old = (data["groups"].get(g, {}) or {}).get("addressing_hit")
            learned.append(g)
            # 显示名跟群名八竿子打不着 = 它顶着别人的备注（备注被打错群）
            if _norm_name(g) not in _norm_name(shown):
                mismatched.append(f"{g} 顶着「{shown}」")
            if shown != old:
                changed.append(f"{g} → 「{shown}」")
                try:
                    registry.mark_addressing(g, shown)
                except Exception as e:
                    log("WARNING", f"回写寻址失败 {g}: {e}")
        else:
            failed.append(f"{g}：{why}")
        time.sleep(0.6)

    lines = [f"查完 {len(specs)} 个群：认出 {len(learned)} 个，其中 {len(changed)} 个的寻址串变了。"]
    if changed:
        lines.append("🔧 新学到/更正的显示名：\n" + "\n".join(f" - {x}" for x in changed[:25])
                     + (f"\n…另有 {len(changed) - 25} 个" if len(changed) > 25 else ""))
    if mismatched:
        lines.append(f"🔴 {len(mismatched)} 个群顶着别人的备注（备注打错群，需人工清空后重打）：\n"
                     + "\n".join(f" - {x}" for x in mismatched[:15]))
    if failed:
        lines.append(f"❌ 仍然认不出的 {len(failed)} 个（可能真退群了，或被打了别人的备注）：\n"
                     + "\n".join(f" - {x}" for x in failed[:20])
                     + (f"\n…另有 {len(failed) - 20} 个" if len(failed) > 20 else ""))
    reply(chat, "\n".join(lines))
    return True


def _bring_wx_front() -> str:
    """把微信主窗口拉到前台并置为活动窗口。

    为什么必须（2026-08-13 实测）：搜索框那个 EditControl【不是常驻控件】，
    窗口不在前台时 UIA 拿不到它 —— wxautox 自己的 SessionBox.search() 和手动遍历
    都会报 Find Control Timeout，看起来像"控件不存在"，其实只是窗口没激活。
    8/11 那次能成功，是因为主窗口刚被 wake_wx 唤起并置前。"""
    try:
        import psutil
        import win32con
        import win32gui
        import win32process
    except Exception as e:
        return f"置前跳过（缺 win32）：{e}"
    found = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                return
            if win32gui.GetWindowText(h) in ("微信", "Weixin", "WeChat"):
                found.append(h)
        except Exception:
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception as e:
        return f"枚举窗口失败：{e}"
    if not found:
        return "没找到微信主窗口（可能又被关进托盘了，跑 wake_wx.py）"
    h = found[0]
    try:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
        time.sleep(0.2)
    except Exception:
        pass
    # SetForegroundWindow 在【非前台进程】里会被 Windows 直接拒绝 —— bot 进程平时不在
    # 前台，所以它基本没用（8/13 实测：主窗口可见、尺寸正常，前台却是 cmd.exe，
    # 搜索框照样找不到）。UIA 的 SwitchToThisWindow 不受这个限制，wxautox 自己
    # 的 _show() 走的也是它。另外别用 wx.Show()：它作用在缓存的旧 HWND 上，
    # 而主窗口句柄会在进程不重启的情况下被销毁重建。
    try:
        from wxautox4 import uia
        uia.ControlFromHandle(h).SwitchToThisWindow()
        time.sleep(0.9)
        return f"主窗口 hwnd={h} 已置前（SwitchToThisWindow）"
    except Exception as e:
        try:
            win32gui.SetForegroundWindow(h)
            time.sleep(0.8)
            return f"主窗口 hwnd={h} 已置前（SetForegroundWindow 兜底）"
        except Exception as e2:
            return f"主窗口 hwnd={h} 置前失败：{e} / {e2}"


def _find_unmarked(bot, chat, arg=None) -> bool:
    """「查新群」：扫一遍微信里的群，把【没打狗标记】的挑出来 —— 那就是还没纳管的。

    判据就一条：显示名里有没有小狗（任意变体，见 audit.DOG_MARKS）。打过标签的群
    显示名以🐶结尾，没打过的还是光秃秃的群名。人照着这张单子决定要不要纳管。

    分两类报，因为处置方式不同：
      · 不在登记表里 → 新群，要先在面板归类（分组/允许转发）再打标签
      · 在登记表里但没标签 → 漏打了，直接「修备注」就能补上
    """
    wx = getattr(bot, "wx", None)
    reply(chat, "开始扫描微信里的群，挑出没打🐶标签的…")
    t0 = time.time()
    try:
        with MAIN_WINDOW_LOCK:
            raw = wx.GetAllRecentGroups()
    except Exception as e:
        reply(chat, f"扫描失败：{e}")
        return True

    names = audit.extract_group_names(raw)
    data = registry.load()
    known = set(data.get("groups", {}))
    # 登记表里的群还可能是靠 addressing_hit 认人的，一并算作"已知"
    known |= {str(g.get("addressing_hit") or "") for g in data.get("groups", {}).values()}
    known |= {audit.strip_dog(x) for x in known if x}
    skip = _admin_group_names(store.load())

    fresh, missed_tag = [], []
    for n in names:
        if not n or n in skip or audit.strip_dog(n) in skip:
            continue
        if audit.has_dog(n):
            continue                      # 打过标签的，不用管
        (missed_tag if (n in known or audit.strip_dog(n) in known) else fresh).append(n)

    lines = [f"扫了 {len(names)} 个群，耗时 {time.time() - t0:.0f} 秒。"]
    if fresh:
        lines.append(f"🆕 {len(fresh)} 个【新群】（登记表里没有，也没打🐶）：\n"
                     + "\n".join(f" - {n}" for n in fresh[:25])
                     + (f"\n…另有 {len(fresh) - 25} 个" if len(fresh) > 25 else "")
                     + "\n处置：先去面板『群列表』归类（选分组 + 勾允许转发），再发「修备注」打标签。")
    if missed_tag:
        lines.append(f"⚠️ {len(missed_tag)} 个群在登记表里【但没打🐶】：\n"
                     + "\n".join(f" - {n}" for n in missed_tag[:25])
                     + (f"\n…另有 {len(missed_tag) - 25} 个" if len(missed_tag) > 25 else "")
                     + "\n处置：直接发「修备注 全部」补打。")
    if not fresh and not missed_tag:
        lines.append("✅ 扫到的群全都打过🐶标签了，没有漏网的。")
    reply(chat, "\n".join(lines))
    return True


def _disable_group(chat, name) -> bool:
    """「禁群 <群名>」：把一个群移出转发列表。

    转发搜不到的群【不再自动禁】——微信搜索会抖，一次失败就禁掉会误伤好群，
    而且没人会立刻发现。所以转发只给清单，由人在微信里确认后用这条指令处置。"""
    got = registry.mark_unreachable(name)
    if got:
        reply(chat, f"「{got}」已移出转发列表。发「恢复群 {got}」可以放回来。")
    else:
        reply(chat, f"登记表里没有「{name}」，名字要跟「群列表」里的一致。")
    return True


def _enable_group(chat, name) -> bool:
    """「恢复群 <群名>」：把之前移出的群放回转发列表，并清掉可能过期的寻址串
    （让它下次「查寻址」重新学一个准的）。"""
    data = registry.load()
    g = data.get("groups", {}).get(name)
    if not g:
        reply(chat, f"登记表里没有「{name}」。")
        return True
    g["allow_forward"] = True
    g["status"] = "active"
    g["addressing_hit"] = None
    registry.save(data)
    reply(chat, f"「{name}」已放回转发列表，寻址串已清空。\n发「查寻址 {name}」重新学一个准的。")
    return True


def _probe_remark_panel(bot, chat, group_name) -> bool:
    """「探备注面板 <群名>」：切到该群、打开"聊天信息"面板，把控件树打出来。【只读】。

    目的：找到备注输入框，验证"自己清空再写"这条路通不通。
    `SetGroupRemark` 对已有备注是【追加】、空串也清不掉，所以打错的备注至今只能人工清；
    但它既然能把字写进去，就说明 wxautox 内部够得着那个输入框 —— 8/3 摸了三轮没找到
    入口（`ChatMoreInfoWnd(wx.ChatBox)` 的 control 是 None、"聊天信息"按钮扫不到），
    现在有两把当时没有的钥匙：窗口置前（不激活时控件会凭空消失，搜索框就是这么骗了我
    两轮），以及 wx.ChatBox.edit_info 这个方法。

    ⚠️ 只 dump 结构，一个字都不写。要动手也必须先拿「肥肉测试1」这类测试群验。"""
    wx = getattr(bot, "wx", None)
    out = []
    with MAIN_WINDOW_LOCK:
        out.append(_bring_wx_front())
        if not _switched(wx, group_name, exact=False):
            reply(chat, f"切不到「{group_name}」，先确认这个群还在")
            return True
        time.sleep(0.6)

        cb = getattr(wx, "ChatBox", None)
        if cb is None:
            reply(chat, "拿不到 wx.ChatBox")
            return True
        out.append("ChatBox 方法：" + ", ".join(a for a in dir(cb) if not a.startswith("_"))[:300])

        # 试着打开"聊天信息"面板 —— edit_info 是最像正门的那个
        opened = None
        for meth in ("edit_info", "get_info"):
            fn = getattr(cb, meth, None)
            if fn is None:
                continue
            done, r, err = _ui_call(fn, 12, f"chatbox.{meth}")
            out.append(f"ChatBox.{meth}() → done={done} err={err!r} 返回={type(r).__name__}:{repr(r)[:160]}")
            if done and err is None:
                opened = r
                break

        # 面板开没开，看微信多了什么窗口 + 把当前控件树里的 Edit 都找出来
        try:
            from wxautox4 import uia
            root = getattr(cb, "control", None)
            edits = []

            def walk(c, depth=0):
                if depth > 8 or len(edits) > 25:
                    return
                try:
                    kids = c.GetChildren()
                except Exception:
                    return
                for ch in kids:
                    try:
                        if ch.ControlTypeName in ("EditControl", "TextControl", "ButtonControl"):
                            nm = (ch.Name or "").strip()
                            if nm:
                                edits.append(f"{'· ' * depth}{ch.ControlTypeName} | {nm[:40]}")
                    except Exception:
                        pass
                    walk(ch, depth + 1)

            if root is not None:
                walk(root)
            out.append(f"ChatBox 里的可交互控件 {len(edits)} 个：")
            out.extend(edits[:25])
            _ = uia
        except Exception as e:
            out.append(f"遍历控件失败：{e}")

    reply(chat, "\n".join(out))
    return True


def _read_search_text(box):
    """读搜索框里当前实际的文字。读不到返回 None（当"没这条信息"，不当失败）。

    uiautomation 的 EditControl 值在 ValuePattern 上；各版本控件类型不一定支持，
    所以两条路都试，全都吞异常 —— 这只是诊断信息，绝不能因为读不到就把主流程弄挂。"""
    if box is None:
        return None
    try:
        return str(box.GetValuePattern().Value or "")
    except Exception:
        pass
    try:
        return str(getattr(box, "Name", "") or "") or None
    except Exception:
        return None


def _bench_search(bot, chat, arg) -> bool:
    """「测搜索 <串>|<轮数>」：同一个查询连打 N 轮，量搜索结果稳不稳、返回有多快。只读。

    ★ 为什么必须黑盒量（2026-08-15，大松看着屏幕提的）：
    肉眼看到"搜完立刻就跳到群里"，怀疑没等结果渲染完就判定。而 wxautox4 是
    **编译发行**（19 个 .pyd，只有 param.py 这种壳是纯 .py），`ChatWith`/`search`
    的实现根本读不到源码 —— 这个怀疑只能靠反复打同一个查询、看返回项数稳不稳来证伪。

    历史上三条独立记录都指着同一件事，但从没有人真去量过：
      · SEARCH_CHAT_TIMEOUT 实测默认 2 秒（文档写 5），"搜索结果常常 2 秒内没渲染完"
      · 拉群专门做了切群重试 3 次，注释写"搜索结果常常不是第一时间出来"
      · 转发失败标记的注释写"微信搜索被实测证明会抖，同一个群这轮搜到下轮搜不到"

    判据：
      · 项数在 0 和 N 之间跳  → 实锤"读太快"，该在搜索后补结算等待
      · 项数稳定、且单次耗时只有几十毫秒 → 它压根没在等渲染，同样是问题
      · 项数稳定、耗时在百毫秒以上 → 搜索本身没问题，失败另有原因
    """
    parts = [p.strip() for p in str(arg or "").split("|")]
    q = parts[0]
    rounds = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8
    rounds = max(1, min(rounds, 20))
    if not q:
        reply(chat, "用法：测搜索 <寻址串>|<轮数>（轮数可省，默认 8）")
        return True

    wx = getattr(bot, "wx", None)
    sb = getattr(wx, "SessionBox", None) if wx else None
    if sb is None or not hasattr(sb, "search"):
        reply(chat, "拿不到 SessionBox.search，没法测")
        return True

    rows, counts, costs = [], [], []
    with MAIN_WINDOW_LOCK:
        try:
            wx.Show()
        except Exception:
            pass
        _bring_wx_front()
        for i in range(rounds):
            keepalive()
            box = getattr(sb, "searchbox", None)
            if box is not None:
                _ui_call(lambda b=box: b.SendKeys("{Ctrl}a{Delete}", waitTime=0.15), 6, "clear")
            t0 = time.time()
            done, res, err = _ui_call(lambda: sb.search(q), 20, f"bench:{i}")
            ms = (time.time() - t0) * 1000
            # ★ 把搜索框里【实际】是什么读回来（2026-08-15 加）：不读的话，
            # "第 6 轮返回 11 项且未命中"有两种解释分不开 —— 搜索吐了中间态，
            # 还是上一轮的清空失败、这一轮搜的其实是"残留+新串"。
            # 两种都是真问题，但修法完全不同，不能靠猜。
            actual = _read_search_text(box)
            tag = "" if (actual is None or actual == q) else f"  ⚠️框里是「{actual}」"
            if not done:
                rows.append(f"  第{i+1}轮  ⏱ 卡住未返回（>20s）{tag}"); counts.append(-1)
            elif err is not None:
                rows.append(f"  第{i+1}轮  ✗ 异常 {err}  {ms:.0f}ms{tag}"); counts.append(-1)
            else:
                n = len(res) if hasattr(res, "__len__") else -1
                hit = any(q in str(getattr(x, "content", "")) for x in (res or []))
                rows.append(f"  第{i+1}轮  {n} 项  {'命中' if hit else '未命中'}  "
                            f"{ms:.0f}ms{tag}")
                counts.append(n); costs.append(ms)
            time.sleep(0.4)
        box = getattr(sb, "searchbox", None)
        if box is not None:
            _ui_call(lambda b=box: b.SendKeys("{Esc}", waitTime=0.2), 6, "esc")

    ok = [c for c in counts if c >= 0]
    uniq = sorted(set(ok))
    verdict = ("⚠️ 项数在几个值之间跳 → 搜索结果渲染没等稳，实锤"
               if len(uniq) > 1 else "项数稳定")
    if len(uniq) <= 1 and costs and sorted(costs)[len(costs) // 2] < 200:
        verdict += "，但单次只要 %.0fms —— 它压根没在等渲染" % (sorted(costs)[len(costs) // 2])
    stat = (f"耗时 min/中位/max = {min(costs):.0f}/{sorted(costs)[len(costs)//2]:.0f}/"
            f"{max(costs):.0f} ms" if costs else "无有效样本")
    flog(f"测搜索「{q}」×{rounds}：项数 {counts}，{stat}")
    reply(chat, f"测搜索「{q}」×{rounds} 轮（只读）：\n" + "\n".join(rows)
                + f"\n\n项数取值：{uniq or '（全失败）'}\n{stat}\n判定：{verdict}")
    return True


def _probe_search(bot, chat, keyword) -> bool:
    """「探搜索 <词>」：在主窗口搜索框里输入关键词，把搜索结果的控件树打出来。只读。

    为什么需要它（2026-08-11）：备注被打错群之后，登记表和微信就对不上了，而
    `ChatInfo()` 只给【显示名】（有备注时就是备注本身），读不到真实群名 —— 所以
    "「A群名🐶」这个备注到底挂在哪个群头上"一直只能靠人一个个去微信里搜。
    但微信的搜索结果里有：有备注的群会多显示一行「群聊名称：真实群名」。
    那正是我们缺的那半边。wxautox 没暴露主窗口搜索，只能自己操作 UI，
    而 UI 代码不摸清控件结构就是瞎写 —— 先用这条只读指令把结构打出来。"""
    wx = getattr(bot, "wx", None)
    if wx is None:
        reply(chat, "拿不到 wx 实例"); return True

    out = []
    with MAIN_WINDOW_LOCK:
        try:
            wx.Show()
        except Exception as e:
            out.append(f"Show() 失败：{e}")
        out.append(_bring_wx_front())
        # 主窗口控件对象藏在哪个属性上，各版本不一样，挨个试；都不行就按句柄取
        root = None
        # wx 上真正挂着 UI 的是这三个 Box（41.x 实测），先看看它们自带什么方法
        for box in ("SessionBox", "NavigationBox", "ChatBox"):
            o = getattr(wx, box, None)
            if o is None:
                continue
            meths = [a for a in dir(o) if not a.startswith("_")]
            out.append(f"wx.{box} → {type(o).__name__}：{', '.join(meths[:18])}")
        for path in ("SessionBox", "NavigationBox", "ChatBox", "control", "core"):
            obj = getattr(wx, path, None)
            if obj is None:
                continue
            cand = getattr(obj, "control", obj)
            if cand is not None and hasattr(cand, "GetChildren"):
                root = cand
                out.append(f"主窗口控件取自 wx.{path}")
                break
        if root is None:
            for owner, label in ((wx, "wx"), (getattr(wx, "core", None), "wx.core")):
                hwnd = getattr(owner, "HWND", None) if owner is not None else None
                if hwnd:
                    try:
                        from wxautox4 import uia
                        root = uia.ControlFromHandle(int(hwnd))
                        out.append(f"主窗口控件按句柄取自 {label}.HWND={hwnd}")
                        break
                    except Exception as e:
                        out.append(f"{label}.HWND={hwnd} 取控件失败：{e}")
        if root is None:
            attrs = [a for a in dir(wx) if not a.startswith("__")][:40]
            reply(chat, "找不到主窗口控件。wx 的属性：\n" + ", ".join(attrs))
            return True

        # wxautox 自带 SessionBox.search()，别自己去摸搜索框控件
        sb = getattr(wx, "SessionBox", None)
        edit = None
        try:
            r = sb.search(keyword)
            time.sleep(1.2)
            out.append(f"SessionBox.search() 返回 {type(r).__name__}，{len(r)} 项")
            for i, el in enumerate(list(r)[:10]):
                attrs = [a for a in dir(el) if not a.startswith("_")]
                out.append(f"[{i}] {type(el).__name__} attrs={attrs}")
                for a in ("name", "Name", "text", "title", "subtitle", "info", "content", "nickname"):
                    if hasattr(el, a):
                        try:
                            out.append(f"      .{a} = {getattr(el, a)!r}")
                        except Exception as e2:
                            out.append(f"      .{a} 读取失败：{e2}")
                # 副标题「群聊名称：xxx」多半是结果项底下的子控件，把子树也打出来
                c = getattr(el, "control", None)
                if c is not None:
                    try:
                        for ch in c.GetChildren():
                            nm = (ch.Name or "").strip()
                            if nm:
                                out.append(f"      child {ch.ControlTypeName}: {nm[:60]}")
                            for gc in ch.GetChildren():
                                gnm = (gc.Name or "").strip()
                                if gnm:
                                    out.append(f"        · {gc.ControlTypeName}: {gnm[:60]}")
                    except Exception as e2:
                        out.append(f"      子控件读取失败：{e2}")
        except Exception as e:
            out.append(f"SessionBox.search 失败：{e}")
            # ★ 别用 root.EditControl(searchDepth=N)：41.x 上它和 wxautox 自己的
            # search() 一样报 Find Control Timeout，可手动遍历树【能】找到那个
            # Name='搜索' 的 EditControl。所以是查找方式失效，不是控件不存在。
            def _find_edit(c, depth=0):
                if depth > 8:
                    return None
                try:
                    kids = c.GetChildren()
                except Exception:
                    return None
                for ch in kids:
                    try:
                        if ch.ControlTypeName == "EditControl":
                            return ch
                    except Exception:
                        pass
                    got = _find_edit(ch, depth + 1)
                    if got is not None:
                        return got
                return None

            try:
                edit = _find_edit(root)
                if edit is None:
                    out.append("遍历树也没找到 EditControl")
                    reply(chat, "\n".join(out)); return True
                out.append(f"遍历树找到搜索框：Name={edit.Name!r}")
                edit.Click(simulateMove=False)
                time.sleep(0.4)
                edit.SendKeys("{Ctrl}a", waitTime=0.1)
                edit.SendKeys(keyword, waitTime=0.4)
                time.sleep(1.5)
                rows2 = []

                def _walk2(c, depth=0):
                    if depth > 9 or len(rows2) > 60:
                        return
                    try:
                        kids = c.GetChildren()
                    except Exception:
                        return
                    for ch in kids:
                        try:
                            nm = (ch.Name or "").strip()
                            if nm:
                                rows2.append("%s%s | %s" % ("· " * depth, ch.ControlTypeName,
                                                            nm.replace("\n", " ⏎ ")[:70]))
                        except Exception:
                            pass
                        _walk2(ch, depth + 1)

                _walk2(root)
                out.append(f"搜「{keyword}」的结果控件 {len(rows2)} 个：")
                out.extend(rows2)
                reply(chat, "\n".join(out)); return True
            except Exception as e2:
                out.append(f"回退手动输入也失败：{e2}")
                reply(chat, "\n".join(out)); return True

        # 把树打出来：只要 Name 非空的，深度 8 以内
        rows = []

        def walk(c, depth=0):
            if depth > 8 or len(rows) > 160:
                return
            try:
                kids = c.GetChildren()
            except Exception:
                return
            for ch in kids:
                try:
                    nm = (ch.Name or "").strip()
                    if nm:
                        rows.append("%s%s | %s" % ("· " * depth, ch.ControlTypeName, nm[:48]))
                except Exception:
                    pass
                walk(ch, depth + 1)

        walk(root)
        out.append(f"搜「{keyword}」后，Name 非空的控件 {len(rows)} 个：")
        out.extend(rows)
        try:                                          # 收尾：别把主窗口留在搜索页
            if edit is not None:
                edit.SendKeys("{Esc}", waitTime=0.2)
            elif getattr(sb, "searchbox", None) is not None:
                sb.searchbox.SendKeys("{Esc}", waitTime=0.2)
        except Exception:
            pass
    reply(chat, "\n".join(out))
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


def _try_edit_remark(bot, chat, arg) -> bool:
    """「试改备注 <群名>|<新备注>」：用 EditFriendInfo 改备注，看它到底是替换还是追加。

    背景：SetGroupRemark 对已有备注是【追加】、传空串也清不掉，所以打错的备注
    只能人工清。但 wxautox 还有另一条路 —— EditFriendInfo(remark=...)，
    它走的是 EditRemarkWindow（有 set_remark + confirm），而微信改备注那个弹窗
    是预填+全选的，很可能是替换语义。这条指令就是拿测试群把它验掉。"""
    wx = getattr(bot, "wx", None)
    parts = [p.strip() for p in (arg or "").split("|")]
    if len(parts) != 2 or not parts[0]:
        reply(chat, "用法：试改备注 <群名>|<新备注>")
        return True
    name, want = parts
    out = []
    with MAIN_WINDOW_LOCK:
        if not _switched(wx, name, exact=False):
            reply(chat, f"切不到「{name}」")
            return True
        try:
            out.append(f"改前：{wx.ChatInfo()!r}")
        except Exception as e:
            out.append(f"改前读不到：{e}")
        try:
            r = wx.EditFriendInfo(remark=want)
            out.append(f"EditFriendInfo(remark={want!r}) -> {r!r}")
        except Exception as e:
            import traceback
            out.append(f"EditFriendInfo 抛错：{e}\n{traceback.format_exc()[-600:]}")
        time.sleep(1.5)
        try:
            out.append(f"改后：{wx.ChatInfo()!r}")
        except Exception as e:
            out.append(f"改后读不到：{e}")
    reply(chat, "\n".join(out))
    return True


def _admin_group_names(cfg) -> set:
    """不该打备注的群：管理群一旦有了备注，微信显示名（chat.who）就变成「群名🐶」，
    而管理群判定是拿 who 跟配置里的名字直接比对的——打上去等于把指令入口关掉。"""
    names = {(cfg or {}).get("admin_group"), store.DEFAULT_CONFIG.get("admin_group")}
    return {n.strip() for n in names if isinstance(n, str) and n.strip()}


def _fix_remarks(bot, chat, scope) -> bool:
    """遍历【微信里实际存在的所有群】，把备注修成「真实群名🐶」。

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
    此时显示名就是真实群名。是否打对了，拿登记表里的群名集合去核。

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
    names = [n for n in names if n not in skip_names and audit.strip_dog(n) not in skip_names]
    known = set(registry.load().get("groups", {}))
    overrides = (store.load().get("remark_overrides") or {})
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
            verdict, detail = audit.plan_remark(real, known, overrides)
            if verdict == audit.FIX_APPLY and not dry:
                ok, why = _do_set_remark(wx, real, detail)
                if not ok:
                    verdict, detail = audit.FIX_FAILED, why
        results.append((real, verdict, detail))
        if verdict == audit.FIX_APPLY and real:
            done_names.append(real)                      # 这次新打的
        elif verdict == audit.FIX_OK:
            done_names.append(real[:-len(audit.DOG)])    # 已达标的（去掉🐶还原群名）
        time.sleep(0.4)

    msg = audit.summarize_fix(results, dry=dry)
    # 原来这里还要把这批群名回写 Notion『群聊列表』（补🐶、新增行）。去 Notion 化后
    # 登记表自己就是真相源，打成功的那笔已由 _do_set_remark → mark_remark_applied
    # 落盘；微信里扫到但登记表里没有的群，走面板「待归类」归类。
    if not dry:
        known = set(registry.load().get("groups", {}))
        unknown = [n for n in done_names if n and audit.strip_dog(n) not in known
                   and n not in known]
        if unknown:
            msg += (f"\n\n以下 {len(unknown)} 个群微信里有、登记表里没有，"
                    f"去面板手工新增并归类：\n"
                    + "\n".join(f"  - {n}" for n in unknown[:20])
                    + ("\n  …" if len(unknown) > 20 else "")
                    + "\n" + panel.panel_url())
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


# ------------------------------------------------------------------ 拉群 / 迎新

def _format_invites(cfg) -> str:
    """拉群关键词现在只有一张表（registry.invite_keywords，面板维护）。
    config.json 里那份老的本地覆盖若还有残留也一并列出来，提示去面板收编。"""
    items = registry.invite_items(registry.load())
    legacy = cfg.get("invite", {}).get("keywords", {})
    if not items and not legacy:
        return ("还没有拉群关键词。去面板「拉群关键词」页添加：\n" + panel.panel_url())
    lines = ["拉群关键词（用户【私聊】我发关键词即可被拉群）："]
    for k, e in items:
        mark = "" if e["enabled"] else "　（已停用）"
        mark += "　⚠️被 config 覆盖" if k in legacy else ""
        lines.append(f"◾ {k} → {e['group']}{mark}")
    if legacy:
        lines.append(f"— config.json 遗留覆盖（{len(legacy)} 条，同名时优先，建议去面板收编）—")
        lines.extend(f"◾ {k} → {v}" for k, v in legacy.items())
    lines.append("面板：" + panel.panel_url())
    return "\n".join(lines)


def _set_remark_override(chat, cfg, group_name, remark) -> bool:
    """「设备注 <群名>|<备注>」：给某个群手工指定要打的备注，「修备注」照它打。

    为什么需要：群名顶到 16 个汉字的群，加🐶必然超过 48 字节上限，硬打会被微信
    截成垃圾（2026-08-03 打坏过两个）。这类只能人来定一个短备注，比如
    「AI+社区：我们到底需要什么样的社区」→「AI+社区：我们到底需要什么样的🐶」。
    留空则取消指定。"""
    if not group_name:
        reply(chat, "格式：设备注 <群名>|<备注>（备注留空=取消指定）")
        return True
    ov = cfg.setdefault("remark_overrides", {})
    if not remark:
        ov.pop(group_name, None)
        store.save(cfg)
        reply(chat, f"已取消「{group_name}」的指定备注，恢复按「群名🐶」打")
        return True
    nbytes = len(remark.encode("utf-8"))
    if nbytes > audit.REMARK_MAX_BYTES:
        reply(chat, f"「{remark}」有 {nbytes} 字节，超过微信备注上限 "
                    f"{audit.REMARK_MAX_BYTES} 字节，打上去会被截成垃圾，换短点的")
        return True
    ov[group_name] = remark
    store.save(cfg)
    reply(chat, f"已指定：「{group_name}」的备注打成「{remark}」（{nbytes} 字节）。\n"
                f"注意这个群现在如果还有旧备注，得先人工清空——SetGroupRemark 是追加。\n"
                f"清完发「修备注 全部」。")
    return True


def _set_invite(chat, cfg, keyword, target) -> bool:
    """「设拉群 <关键词>|<目标群>」——现在直写 registry，和面板同一张表。

    以前它写的是 config.json 的本地覆盖层，于是同一个关键词可能在两处各有一份，
    面板上改了却被 config 那份盖掉。现在只有一张表，群里改和面板改等价。"""
    if not keyword or not target:
        reply(chat, "格式：设拉群 <关键词>|<目标群>"); return True
    try:
        registry.set_invite_keyword(keyword, target)
    except ValueError as e:
        reply(chat, f"没设成：{e}。目标群得先在登记表里（面板「群列表」能看到）。")
        return True
    legacy = (cfg.get("invite") or {}).get("keywords") or {}
    extra = "\n⚠️ config.json 里还有一条同名的旧覆盖，会盖住这次设置，去面板清一下。" \
        if keyword in legacy else ""
    reply(chat, f"已设置：发「{keyword}」→ 拉进「{target}」。{extra}"); return True


def _delete_invite(chat, cfg, keyword) -> bool:
    try:
        registry.delete_invite_keyword(keyword)
    except ValueError as e:
        reply(chat, f"{e}"); return True
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
