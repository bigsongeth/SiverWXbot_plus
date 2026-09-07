# -*- coding: utf-8 -*-
"""
tap.py —— 常驻在 bot 进程里的「开窗现场录像机」。

2026-09-03 的独立进程实验（diag_open_window.py）做了 24 轮：空载、CPU 打满、主窗口 1500x1048，
24/24 全成功。也就是说 MoveWindow 1400 只在生产上下文里出现（进程跑了很久、开着 4~5 个
子窗口、刚扫到新私聊立刻开窗）。再往外拿是拿不到的，只能在它真实失败的那一刻把现场录下来。

原理（和 diag_open_window 一样）：wxautox 41.x 的双击是 `uiplug.Win32.click_by_bbox` 往句柄
发消息，底层走 `win32api.SendMessage`（实测 5 条：按下/抬起、按下/抬起、WM_LBUTTONDBLCLK）。
`uiplug.pyd` 是编译的，但它在调用时才从 `win32gui` / `win32api` / `ctypes.windll.user32`
取函数，所以把这些模块上的函数包一层就能看见它发了什么。

★ 2026-09-08 01:00 真·根因（独立进程 A/B，16 轮，想坏就坏想好就好）：
  **真实鼠标指针停在任何一个微信顶层窗口的顶边那几个像素上**（y=窗口顶+0/+2 坏，+10 就好；
  子窗口顶边一样有毒），wxautox 的假双击就永远不被当双击（微信处理 DBLCLK 只花 0.03–0.05s，
  正常 0.24s），单击照常生效。指针在窗口内部、在子窗口内部、在 Chrome/任务栏上都没事。
  RustDesk 的远端指针经常被留在屏幕顶边（本机 09-08 00:46 实测停在 (1098,0)），于是"白天有人连过就坏、
  重启第一个成功（走搜索菜单路径）其余全败、真实单击有时能救（指针被留在窗口里）有时不能
  （生产 unstick 点完把指针放回顶边）"全对上了。09-06 那套"监听线程交错"的结论很可能是被这个混淆的。
  修法就在 `guard_cursor`：每次 AddListenChat 前看一眼指针，压在微信窗口边框带上就挪进窗口内部，不点击。

安全边界：
- 包装函数只在 `AddListenChat` 执行期间往列表里 append，其余时间是纯透传，
  任何异常都吞掉，绝不改变原调用的返回值。
- 截图只在失败时做一次（PIL.ImageGrab，约 200ms）。
- 对 `wx.AddListenChat` 的包装是实例属性，幂等（重复 install 不会套两层）。
- 全部落盘在 `data/tap-YYYYMMDD.jsonl`，失败截图 `data/tap-shot-*.png`；写盘失败只记日志。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from datetime import datetime

from .config import DATA_DIR, load
from .probe import log, _wx_top_windows, _env_snapshot

MSG_NAMES = {
    0x0006: "WM_ACTIVATE", 0x0007: "WM_SETFOCUS", 0x0021: "WM_MOUSEACTIVATE",
    0x0100: "WM_KEYDOWN", 0x0101: "WM_KEYUP", 0x0102: "WM_CHAR",
    0x0200: "WM_MOUSEMOVE", 0x0201: "WM_LBUTTONDOWN", 0x0202: "WM_LBUTTONUP",
    0x0203: "WM_LBUTTONDBLCLK", 0x0204: "WM_RBUTTONDOWN", 0x0205: "WM_RBUTTONUP",
    0x0206: "WM_RBUTTONDBLCLK", 0x020A: "WM_MOUSEWHEEL", 0x0010: "WM_CLOSE",
    0x0112: "WM_SYSCOMMAND",
}
MOUSE_MSGS = {0x0200, 0x0201, 0x0202, 0x0203, 0x0204, 0x0205, 0x0206}

CALLS: list = []              # 当前这次 AddListenChat 期间抓到的底层调用
_T0 = [0.0]                   # 本次起点，用于相对时间
_ACTIVE = [False]             # 只有 AddListenChat 执行期间才录
_TAPS_INSTALLED = [False]     # 底层函数只包一次（进程级）
_LOCK = threading.Lock()
# 实验开关：AddListenChat 期间把 wxautox 用 SendMessage 发的鼠标按键消息改成 PostMessage。
# 动机（2026-09-06 录像）：失败时 5 条消息和成功时逐字节相同，差别只在微信怎么解读。
# SendMessage 是同步直送、不进消息队列，Qt 给它打的时间戳取自 GetMessageTime()，
# 即这个线程【上一条从队列取出的消息】的时间——而 Qt 判双击靠的正是两次按下的时间戳差。
# 队列里有别的消息穿插时时间戳就乱了，双击不被承认，窗口不建。PostMessage 进队列、带真实时间戳。
_POST_CLICKS = [False]
_CLICK_MSGS = (0x0201, 0x0202, 0x0203)
_ORIG_POST = {}               # {'win32api': 原 PostMessage}，转发时用原函数，不经过录制包装
# 实验钩子（只给 diag 用）：callable(args) -> True 表示"我已经自己处理了这次点击，吞掉原消息"
_INTERCEPT = [None]


def _fmt(x):
    try:
        if isinstance(x, (int, float, str, bool)) or x is None:
            return x
        return repr(x)[:60]
    except Exception:
        return "?"


def decode(tag, args) -> dict:
    """把 (hwnd, msg, wParam, lParam) 翻成人能读的：消息名 + 客户区坐标 + 线程名。"""
    row = {"t": round(time.perf_counter() - _T0[0], 4), "call": tag,
           "args": [_fmt(a) for a in args[:4]], "thread": threading.current_thread().name}
    try:
        if len(args) >= 4 and isinstance(args[1], int):
            msg = args[1]
            row["msg"] = MSG_NAMES.get(msg, hex(msg))
            row["hwnd"] = int(args[0]) if args[0] is not None else None
            if msg in MOUSE_MSGS and isinstance(args[3], int):
                lp = args[3] & 0xFFFFFFFF
                x = lp & 0xFFFF
                y = (lp >> 16) & 0xFFFF
                row["x"] = x - 0x10000 if x >= 0x8000 else x
                row["y"] = y - 0x10000 if y >= 0x8000 else y
    except Exception:
        pass
    return row


def wrap(obj, name, tag) -> bool:
    """把 obj.name 换成会记录的包装；原函数的返回值/异常原样透传。"""
    orig = getattr(obj, name, None)
    if orig is None:
        return False
    convertible = name == "SendMessage"    # 只有 SendMessage 的鼠标消息会被实验开关改成 PostMessage

    def wrapper(*a, **k):
        used_tag = tag
        try:
            if (convertible and _ACTIVE[0] and _INTERCEPT[0] is not None and len(a) >= 4
                    and isinstance(a[1], int) and a[1] in _CLICK_MSGS):
                try:
                    if _INTERCEPT[0](a):
                        used_tag = tag + "→intercepted"
                        return 0
                except Exception:
                    pass
            if (convertible and _ACTIVE[0] and _POST_CLICKS[0] and len(a) >= 4
                    and isinstance(a[1], int) and a[1] in _CLICK_MSGS and _ORIG_POST.get(tag.split(".")[0])):
                used_tag = tag + "→PostMessage"
                _ORIG_POST[tag.split(".")[0]](a[0], a[1], a[2], a[3])
                return 0
            return orig(*a, **k)
        finally:
            if _ACTIVE[0]:
                try:
                    CALLS.append(decode(used_tag, a))
                except Exception:
                    pass

    wrapper._lh_tap_orig = orig      # 单测和排查用：能找回原函数
    try:
        setattr(obj, name, wrapper)
        return True
    except Exception:
        return False


def install_taps() -> list:
    """给所有可能承载"点击"的底层入口打桩，返回成功打上的名单。非 Windows 返回空列表。"""
    if _TAPS_INSTALLED[0]:
        return ["(already)"]
    try:
        import win32api
        import win32gui
    except Exception:
        return []
    done = []
    for mod, mname in ((win32gui, "win32gui"), (win32api, "win32api")):
        if getattr(mod, "PostMessage", None) is not None:
            _ORIG_POST[mname] = mod.PostMessage     # 先存原函数，再包装
        for fn in ("PostMessage", "SendMessage", "SetCursorPos", "mouse_event"):
            if wrap(mod, fn, f"{mname}.{fn}"):
                done.append(f"{mname}.{fn}")
    try:
        u32 = ctypes.windll.user32
        for fn in ("PostMessageW", "PostMessageA", "SendMessageW", "SendMessageA",
                   "SendMessageTimeoutW", "SendInput", "mouse_event", "SetCursorPos",
                   "SetForegroundWindow"):
            try:
                getattr(u32, fn)      # 触发 ctypes 把 _FuncPtr 缓存到实例上
            except Exception:
                continue
            if wrap(u32, fn, f"user32.{fn}"):
                done.append(f"user32.{fn}")
    except Exception:
        pass
    _TAPS_INSTALLED[0] = True
    return done


def _wx_titles(wins):
    return [(x.get("t"), x.get("v"), x.get("r")) for x in (wins or [])
            if x.get("cls") == "Qt51514QWindowIcon"]


def _screenshot(path: str) -> bool:
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
        return True
    except Exception as e:
        log("WARNING", f"录像机截屏失败: {e!r}")
        return False


def _record(row: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"tap-{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log("WARNING", f"录像机写盘失败：{e}")


def _is_failure(result, err) -> bool:
    if err is not None:
        return True
    try:
        return not bool(result)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 复位（2026-09-06 16:24 实测钉死的修法）
#
# 坏状态：wxautox 监听线程往微信 UI 线程高密度灌假点击之后，微信内部的鼠标/按压状态卡住，
# 此后哪怕 UI 线程完全空闲（CPU 0%、WM_NULL 往返 <1ms），双击也再也不算数，独立窗口永远开不出来。
# 试过的复位动作：Esc（把主窗口收进托盘了，别用）、SwitchToThisWindow 激活、WM_CANCELMODE 都没用；
# 一次【真实鼠标单击】微信任一窗口的空白处立刻恢复（2/2）。minmax（最小化再还原）见 diag 结果。
# ---------------------------------------------------------------------------
_WX_PROCS = ("weixin.exe", "wechat.exe")


def _wx_main_hwnd():
    try:
        return (_env_snapshot() or {}).get("wx_main_hwnd")
    except Exception:
        return None


def _point_owner_is_wechat(sx: int, sy: int) -> bool:
    """屏幕点 (sx,sy) 最上面那个窗口是不是微信的——点下去必须落在微信身上，别点到别的程序。"""
    try:
        import ctypes as _ct
        import psutil
        import win32gui
        import win32process

        class _PT(_ct.Structure):
            _fields_ = [("x", _ct.c_long), ("y", _ct.c_long)]
        u32 = _ct.windll.user32
        u32.WindowFromPoint.argtypes = [_PT]
        u32.WindowFromPoint.restype = _ct.c_void_p
        h = u32.WindowFromPoint(_PT(sx, sy))
        if not h:
            return False
        root = win32gui.GetAncestor(int(h), 2)
        _, pid = win32process.GetWindowThreadProcessId(root)
        return psutil.Process(pid).name().lower() in _WX_PROCS
    except Exception:
        return False


def unstick(mode: str, hwnd=None, points=None) -> str:
    """把微信从坏状态拉回来。返回一句人读的说明；失败抛异常。

    mode='click'：在主窗口客户区若干候选空白点里挑第一个"最上面确实是微信窗口"的点，
                  用真实鼠标（mouse_event）单击一下，**指针留在那里**。
                  2026-09-08 复盘：它之所以"有时有效"，其实是把指针从窗口顶边挪进了窗口内部；
                  现在 guard_cursor 在开窗前就做这件事，这里只是兜底。
    mode='minmax'：主窗口最小化再还原，不碰鼠标。
    """
    import win32api
    import win32con
    import win32gui
    hwnd = hwnd or _wx_main_hwnd()
    if not hwnd:
        raise RuntimeError("找不到微信主窗口")
    if mode == "minmax":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(1)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1)
        return "主窗口最小化再还原"
    if mode == "click":
        # 候选点：聊天区下方空白 / 聊天区中部 / 左侧导航栏下方空白（客户区坐标）
        cands = points or [(600, 900), (700, 600), (37, 700)]
        # 点完【不再】把指针放回原处（2026-09-08）：原处多半就是屏幕顶边——09-08 00:27 12 次复位全废的原因。
        for cx, cy in cands:
            try:
                sx, sy = win32gui.ClientToScreen(hwnd, (int(cx), int(cy)))
            except Exception:
                continue
            if not _point_owner_is_wechat(sx, sy):
                continue
            win32api.SetCursorPos((sx, sy))
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.3)
            return f"真实鼠标单击微信窗口空白处 ({sx},{sy})，指针留在这里"
        raise RuntimeError("候选点上面都不是微信窗口，没点")
    raise ValueError(f"不认识的复位模式 {mode!r}")


# ---------------------------------------------------------------------------
# 指针守卫（2026-09-08 钉死的根因，见文件头）：AddListenChat 之前把压在微信窗口边框带上的真实指针挪开。
# 只移动、不点击；指针在窗口内部 / 别的程序上一律不碰（人正在用鼠标时不打扰）。
# ---------------------------------------------------------------------------
CURSOR_BAND_DEFAULT = 8        # 离窗口任一条边 ≤ 8px 算边框带（实测顶边 +2 坏、+10 好；其余三边按同样宽度保守处理）
_MAIN_TITLES = ("微信", "Weixin", "WeChat")
_LAST_GUARD = [None]           # 最近一次守卫动作，供 _dump 写进记录


def _cursor_pos():
    import win32api
    return tuple(win32api.GetCursorPos())


def _set_cursor_pos(pt):
    import win32api
    win32api.SetCursorPos((int(pt[0]), int(pt[1])))


def _big_visible_wx_windows(wins):
    out = []
    for w in wins or []:
        r = w.get("r") or []
        if w.get("cls") == "Qt51514QWindowIcon" and w.get("v") and len(r) == 4 \
                and r[2] - r[0] >= 200 and r[3] - r[1] >= 200:
            out.append(w)
    return out


def cursor_danger(cursor, wins, band: int = CURSOR_BAND_DEFAULT):
    """真实指针是否压在某个可见微信顶层窗口的边框带上。返回命中的窗口 dict，安全返回 None。

    wins 按 Z 序（EnumWindows 从上往下），取【第一个】包含该点的可见微信窗口来判——
    它就是指针真正压着的那个（生产里主窗口和子窗口 rect 相同，上面的那个说了算）。纯函数，单测直接喂。
    """
    if not cursor or not wins:
        return None
    x, y = cursor
    for w in _big_visible_wx_windows(wins):
        l, t, r, b = w["r"]
        if not (l <= x < r and t <= y < b):
            continue
        if (x - l) <= band or (r - 1 - x) <= band or (y - t) <= band or (b - 1 - y) <= band:
            return w
        return None
    return None


def safe_park_point(wins):
    """挑一个"肯定安全"的落点：微信主窗口（找不到就任一大窗口）内部、离边 ≥ 40px 的聊天区。
    实测 (700,600) / (608,900) 这类窗口内部点、哪怕上面盖着子窗口，双击都正常。"""
    big = _big_visible_wx_windows(wins)
    if not big:
        return None
    main = next((w for w in big if w.get("t") in _MAIN_TITLES), big[0])
    l, t, r, b = main["r"]
    w_, h_ = r - l, b - t
    x = l + max(40, min(600, w_ // 2))
    y = t + max(40, min(int(h_ * 0.85), h_ - 40))
    return (x, y)


def guard_cursor(tcfg: dict | None = None):
    """AddListenChat 前调一次。压在边框带上就挪到安全点，返回 {'from','to','win'}；没动返回 None。
    全程吞异常——它只是保险，绝不能把开窗流程炸掉。"""
    _LAST_GUARD[0] = None
    tcfg = tcfg or {}
    if not tcfg.get("cursor_guard", True):
        return None
    try:
        cur = _cursor_pos()
        if not _point_owner_is_wechat(cur[0], cur[1]):     # 指针在别的程序上（Chrome/任务栏），安全
            return None
        wins = _wx_top_windows() or []
        hit = cursor_danger(cur, wins, int(tcfg.get("cursor_band", CURSOR_BAND_DEFAULT)))
        if not hit:
            return None
        to = safe_park_point(wins)
        if not to:
            return None
        _set_cursor_pos(to)
        time.sleep(0.15)
        info = {"from": list(cur), "to": list(to), "win": hit.get("t")}
        _LAST_GUARD[0] = info
        log("WARNING", f"录像机：真实指针停在「{hit.get('t')}」边框带上 {list(cur)}，微信会吞掉双击，"
                       f"已挪到窗口内部 {list(to)}（不点击）")
        return info
    except Exception as e:
        log("WARNING", f"录像机：指针守卫失败（忽略）：{e!r}")
        return None


def wrap_add_listen_chat(wx, tcfg: dict | None = None):
    """给 wx.AddListenChat 套一层录像。幂等：已经套过就原样返回。"""
    orig = getattr(wx, "AddListenChat", None)
    if orig is None or getattr(orig, "_lh_tap", False):
        return orig
    tcfg = tcfg or {}
    want_shot = bool(tcfg.get("screenshot", True))
    keep_ok = bool(tcfg.get("keep_success", True))

    def wrapped(*a, **k):
        nickname = k.get("nickname", a[0] if a else None)
        # 同一时刻只可能有一个 AddListenChat（wxautox 自己有 uilock），但求稳还是上锁
        with _LOCK:
            before = None
            try:
                before = _wx_top_windows()
            except Exception:
                pass
            # 实验开关每次热读：改 data/config.json 的 tap.post_clicks 下一条消息就生效，不用重启
            try:
                _POST_CLICKS[0] = bool(((load().get("tap") or {}).get("post_clicks", False)))
            except Exception:
                pass
            guard_cursor(tcfg)          # 根因修法：指针压在微信窗口边框带上就先挪开，见文件头
            CALLS.clear()
            _T0[0] = time.perf_counter()
            _ACTIVE[0] = True
            t0 = time.time()
            err = None
            result = None
            try:
                result = orig(*a, **k)
                return result
            except Exception as e:
                err = repr(e)
                # 复位后原地重试一次：只对「开不出窗口」这一种失败做，别的异常原样抛
                mode = str(tcfg.get("unstick", "click") or "")
                if mode and "MoveWindow" in err:
                    _ACTIVE[0] = False
                    calls_first = list(CALLS)
                    try:
                        how = unstick(mode, points=tcfg.get("unstick_points"))
                        log("WARNING", f"录像机：{nickname} 开窗失败，已做复位（{how}），原地重试一次")
                    except Exception as ue:
                        log("WARNING", f"录像机：{nickname} 开窗失败，复位动作失败（{ue!r}），不重试")
                        raise e
                    try:
                        _dump(nickname, True, err, round(time.time() - t0, 2), calls_first, before, want_shot,
                              note="复位前的那次")
                    except Exception:
                        pass
                    CALLS.clear()
                    _T0[0] = time.perf_counter()
                    _ACTIVE[0] = True
                    t0 = time.time()
                    try:
                        result = orig(*a, **k)
                        err = None
                        log("INFO", f"录像机：{nickname} 复位后重试成功")
                        return result
                    except Exception as e2:
                        err = repr(e2)
                        log("WARNING", f"录像机：{nickname} 复位后重试仍失败：{err}")
                        raise
                raise
            finally:
                _ACTIVE[0] = False
                cost = round(time.time() - t0, 2)
                calls = list(CALLS)
                failed = _is_failure(result, err)
                if failed or keep_ok:
                    try:
                        _dump(nickname, failed, err, cost, calls, before, want_shot)
                    except Exception as e:
                        log("WARNING", f"录像机记录失败：{e!r}")

    wrapped._lh_tap = True
    wrapped._lh_tap_orig = orig
    try:
        wx.AddListenChat = wrapped
    except Exception as e:
        log("WARNING", f"录像机包装 AddListenChat 失败：{e!r}")
        return orig
    return wrapped


def _dump(nickname, failed, err, cost, calls, before, want_shot, note=None):
    after = None
    env = {}
    shot = None
    if failed:
        try:
            after = _wx_top_windows()
        except Exception:
            pass
        try:
            env = _env_snapshot() or {}
        except Exception:
            env = {}
        if want_shot:
            shot = os.path.join(DATA_DIR, f"tap-shot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
            if not _screenshot(shot):
                shot = None
    mouse = [c for c in calls if c.get("msg") in ("WM_LBUTTONDOWN", "WM_LBUTTONUP", "WM_LBUTTONDBLCLK")]
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nickname": nickname, "ok": not failed, "err": err, "cost_sec": cost,
        "n_calls": len(calls), "n_click_msgs": len(mouse), "calls": calls,
        "wx_before": _wx_titles(before) if before is not None else None,
        "wx_after": _wx_titles(after) if after is not None else None,
        "fg": env.get("fg_title"), "fg_proc": env.get("fg_proc"),
        "main_rect": env.get("wx_main_rect"), "rustdesk_conns": env.get("rustdesk_conns"),
        "hit_test": env.get("hit_test"), "shot": shot, "note": note,
        "cursor": env.get("cursor"), "cursor_win": env.get("cursor_win"),
        "cursor_guard": _LAST_GUARD[0],
    }
    _record(row)
    if failed:
        summary = "; ".join(f"t+{c['t']:.3f} {c.get('msg')} hwnd={c.get('hwnd')} ({c.get('x')},{c.get('y')})"
                            for c in mouse[:6]) or "（没录到任何点击消息）"
        log("WARNING", f"录像机：{nickname} 开窗失败已存档，点击序列 {len(mouse)} 条：{summary}"
                       f"{'，截图 ' + os.path.basename(shot) if shot else ''}")


def install(bot) -> bool:
    """由 probe.register 调用。读 config 的 tap 段；关了就什么都不做。"""
    cfg = load()
    tcfg = cfg.get("tap", {}) or {}
    if not tcfg.get("enabled", True):
        log("INFO", "开窗录像机已关闭，不挂载")
        return False
    wx = getattr(bot, "wx", None)
    if wx is None:
        log("WARNING", "开窗录像机：bot.wx 为空，不挂载")
        return False
    taps = install_taps()
    _POST_CLICKS[0] = bool(tcfg.get("post_clicks", False))
    if _POST_CLICKS[0]:
        log("INFO", "开窗录像机：实验开关 post_clicks 已打开，AddListenChat 期间鼠标消息改走 PostMessage")
    wrapped = wrap_add_listen_chat(wx, tcfg)
    ok = bool(wrapped is not None and getattr(wrapped, "_lh_tap", False))
    log("INFO", f"开窗录像机已挂载：底层打桩 {len([t for t in taps if t != '(already)'])} 处"
                f"{'（沿用）' if taps == ['(already)'] else ''}，AddListenChat {'已包装' if ok else '未包装'}")
    return ok
