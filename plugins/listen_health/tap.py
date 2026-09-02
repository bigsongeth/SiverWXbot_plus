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

    def wrapper(*a, **k):
        try:
            return orig(*a, **k)
        finally:
            if _ACTIVE[0]:
                try:
                    CALLS.append(decode(tag, a))
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


def _dump(nickname, failed, err, cost, calls, before, want_shot):
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
        "hit_test": env.get("hit_test"), "shot": shot,
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
    wrapped = wrap_add_listen_chat(wx, tcfg)
    ok = bool(wrapped is not None and getattr(wrapped, "_lh_tap", False))
    log("INFO", f"开窗录像机已挂载：底层打桩 {len([t for t in taps if t != '(already)'])} 处"
                f"{'（沿用）' if taps == ['(already)'] else ''}，AddListenChat {'已包装' if ok else '未包装'}")
    return ok
