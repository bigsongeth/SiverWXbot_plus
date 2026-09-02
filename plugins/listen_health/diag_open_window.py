# -*- coding: utf-8 -*-
"""
diag_open_window.py —— 把「AddListenChat 弹不出独立窗口（MoveWindow 1400）」这个黑盒打透明。

★ 必须在会话 2 跑（SWXRun），且机器人已停（面板 /stop_bot）。两个进程抢同一个微信 = 结果作废。
★ 不碰 wxautox 源码（都是 .pyd），只在【它调用的底层模块】上打桩。

背景（2026-09-02 复盘 1154 个探针样本 + 8 月全部日志得出的结论，见 CLAUDE.md 3.18）：
  - 失败 = 微信压根没建出新窗口（顶层窗口数前后不变），不是"建了但没找到"。
  - 已证伪：前台是谁 / 会话列表被谁盖住 / 别的线程并发操作 UI / 「找到当前会话」分支 /
    重启能治（8-12 重启→第 1 次成功→第 2 次起继续失败，4 天重启 20 次）。
  - 41.x 的双击是 `uiplug.Win32.click_by_bbox(double_click=True)` 往句柄【发消息】，
    不是真鼠标 —— 所以鼠标/前台类假说全都不相关，而"发了什么消息、发给谁、间隔多久"
    从来没人看过。本脚本就是补这一眼。

它干三件事：
  1. 打桩：win32gui / win32api / ctypes user32 上的 PostMessage/SendMessage/SendInput/
     mouse_event/SetCursorPos 全部包一层，AddListenChat 期间每次调用记
     「往哪个 hwnd、什么消息、坐标（从 lParam 反算）、相对时间」。
     uiplug.pyd 是在调用时才从模块取属性，所以打桩有效；某条通道一条都没记到，
     说明它没走这条路 —— 这同样是信息。
  2. 循环 N 次：AddListenChat(靶子) → GetSubWindow 校验 → RemoveListenChat。
     每轮记成败、耗时、微信顶层窗口清单前后、CPU 负载、DoubleClickTime、主窗口 rect、
     以及第 1 步抓到的调用序列。失败那轮截屏。
  3. 可选 A/B：
       --load K     起 K 个吃满 CPU 的子进程再跑（验"双击两下之间被拖慢、微信没认成双击"）
       --resize WxH 先把主窗口调到指定大小再跑（验"1500x1048 时失败率 31%、1040x736 时 2%"）

跑法（会话 2）：
  把 C:\\Users\\Admin\\swx_payload.cmd（或 swx_run.cmd 的 payload 段）改成：
      set PYTHONIOENCODING=utf-8
      cd /d C:\\Users\\Admin\\SiverWXbot_plus-main
      python plugins\\listen_health\\diag_open_window.py --n 30 --interval 5
  然后 schtasks /run /tn SWXRun。跑完再来一轮 --load 3，两轮对比。

输出：plugins/listen_health/data/diag-<时间戳>.jsonl（逐轮）+ 同名 .log（人读）
      失败截图 data/diag-shot-<时间戳>-<轮次>.png
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from plugins.listen_health.config import DATA_DIR          # noqa: E402
from plugins.listen_health.probe import _wx_top_windows, _env_snapshot  # noqa: E402

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
OUT_JSONL = os.path.join(DATA_DIR, f"diag-{STAMP}.jsonl")
OUT_LOG = os.path.join(DATA_DIR, f"diag-{STAMP}.log")

MSG_NAMES = {
    0x0006: "WM_ACTIVATE", 0x0007: "WM_SETFOCUS", 0x0021: "WM_MOUSEACTIVATE",
    0x0100: "WM_KEYDOWN", 0x0101: "WM_KEYUP", 0x0102: "WM_CHAR",
    0x0200: "WM_MOUSEMOVE", 0x0201: "WM_LBUTTONDOWN", 0x0202: "WM_LBUTTONUP",
    0x0203: "WM_LBUTTONDBLCLK", 0x0204: "WM_RBUTTONDOWN", 0x0205: "WM_RBUTTONUP",
    0x0206: "WM_RBUTTONDBLCLK", 0x020A: "WM_MOUSEWHEEL", 0x0010: "WM_CLOSE",
    0x0112: "WM_SYSCOMMAND",
}
MOUSE_MSGS = {0x0200, 0x0201, 0x0202, 0x0203, 0x0204, 0x0205, 0x0206}

CALLS: list = []          # 当前这一轮抓到的底层调用
_T0 = [0.0]               # 本轮起点，用于相对时间


def w(line: str = "") -> None:
    s = str(line)
    try:
        with open(OUT_LOG, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass
    try:
        print(s)
    except Exception:
        pass


def _fmt(x):
    try:
        if isinstance(x, (int, float, str, bool)) or x is None:
            return x
        return repr(x)[:60]
    except Exception:
        return "?"


def _decode(tag, args):
    """把 (hwnd, msg, wParam, lParam) 翻成人能读的：消息名 + 客户区坐标。"""
    row = {"t": round(time.perf_counter() - _T0[0], 4), "call": tag, "args": [_fmt(a) for a in args[:4]]}
    try:
        if len(args) >= 4 and isinstance(args[1], int):
            msg = args[1]
            row["msg"] = MSG_NAMES.get(msg, hex(msg))
            row["hwnd"] = int(args[0]) if args[0] is not None else None
            if msg in MOUSE_MSGS and isinstance(args[3], int):
                lp = args[3] & 0xFFFFFFFF
                x = lp & 0xFFFF
                y = (lp >> 16) & 0xFFFF
                # 客户区坐标是有符号 16 位
                row["x"] = x - 0x10000 if x >= 0x8000 else x
                row["y"] = y - 0x10000 if y >= 0x8000 else y
    except Exception:
        pass
    return row


def _wrap(obj, name, tag):
    orig = getattr(obj, name, None)
    if orig is None:
        return False

    def wrapper(*a, **k):
        try:
            return orig(*a, **k)
        finally:
            try:
                CALLS.append(_decode(tag, a))
            except Exception:
                pass

    try:
        setattr(obj, name, wrapper)
        return True
    except Exception:
        return False


def install_taps() -> list:
    """给所有可能承载"点击"的底层入口打桩，返回成功打上的名单。"""
    import win32api
    import win32gui
    done = []
    for mod, mname in ((win32gui, "win32gui"), (win32api, "win32api")):
        for fn in ("PostMessage", "SendMessage", "SetCursorPos", "mouse_event"):
            if _wrap(mod, fn, f"{mname}.{fn}"):
                done.append(f"{mname}.{fn}")
    u32 = ctypes.windll.user32
    for fn in ("PostMessageW", "PostMessageA", "SendMessageW", "SendMessageA",
               "SendMessageTimeoutW", "SendInput", "mouse_event", "SetCursorPos",
               "SetForegroundWindow"):
        try:
            getattr(u32, fn)          # 触发 ctypes 把 _FuncPtr 缓存到实例上
        except Exception:
            continue
        if _wrap(u32, fn, f"user32.{fn}"):
            done.append(f"user32.{fn}")
    return done


def _main_hwnd():
    try:
        return (_env_snapshot() or {}).get("wx_main_hwnd")
    except Exception:
        return None


def _screenshot(path: str) -> bool:
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
        return True
    except Exception as e:
        w(f"  截屏失败: {e!r}")
        return False


def _spawn_load(k: int) -> list:
    procs = []
    for _ in range(max(0, k)):
        try:
            procs.append(subprocess.Popen([sys.executable, "-c", "while True: pass"],
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        except Exception as e:
            w(f"起负载子进程失败: {e!r}")
    return procs


def _resize_main(spec: str) -> None:
    import win32gui
    try:
        wdt, hgt = (int(v) for v in spec.lower().split("x"))
    except Exception:
        w(f"--resize 参数不认识: {spec!r}（要 1500x1048 这种）")
        return
    h = _main_hwnd()
    if not h:
        w("找不到微信主窗口，跳过 resize")
        return
    win32gui.ShowWindow(h, 9)  # SW_RESTORE，最大化状态下 MoveWindow 不生效
    win32gui.MoveWindow(h, 0, 0, wdt, hgt, True)
    time.sleep(1)
    w(f"主窗口已调整为 {wdt}x{hgt}，当前 rect={win32gui.GetWindowRect(h)}")


def _wx_titles(wins):
    return [(x.get("t"), x.get("v"), x.get("r")) for x in (wins or [])
            if x.get("cls") == "Qt51514QWindowIcon"]


def _logged_in(wins) -> bool:
    """微信还在登录态吗：要有一个可见的、像样大小的主窗口（标题 微信/WeChat）。

    2026-09-03 00:08 实测：被踢下线后主窗口缩成 296x388 的登录框，旁边还多一个 330x219 的
    「Weixin」提示框。上一版脚本没这道门，对着登录界面又连戳了 7 轮——没意义，也不谨慎。
    """
    for x in wins or []:
        if x.get("cls") != "Qt51514QWindowIcon" or not x.get("v"):
            continue
        if x.get("t") not in ("微信", "WeChat"):
            continue
        r = x.get("r") or [0, 0, 0, 0]
        if (r[2] - r[0]) >= 600 and (r[3] - r[1]) >= 500:
            return True
    return False


def run(args) -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    open(OUT_LOG, "w", encoding="utf-8").close()
    w(f"# diag_open_window @ {datetime.now():%Y-%m-%d %H:%M:%S}  args={vars(args)}")

    # 自检：是不是在会话 2。会话 0 里 EnumWindows 看不到微信，跑了也是白跑。
    wins0 = _wx_top_windows() or []
    if not any(x.get("cls") == "Qt51514QWindowIcon" for x in wins0):
        w("!! 枚举不到任何微信顶层窗口 —— 多半没在会话 2（SSH 是 session 0），停。")
        return 2
    w(f"微信顶层窗口: {_wx_titles(wins0)}")
    if not _logged_in(wins0):
        w("!! 微信主窗口不在登录态（没有 ≥600x500 的可见主窗口），不开始。")
        return 3

    import psutil
    psutil.cpu_percent(interval=None)   # 预热，之后每次调用给的是区间均值
    w(f"DoubleClickTime={ctypes.windll.user32.GetDoubleClickTime()} ms  "
      f"屏幕={ctypes.windll.user32.GetSystemMetrics(0)}x{ctypes.windll.user32.GetSystemMetrics(1)}")

    taps = install_taps()
    w(f"打桩成功: {taps}")

    from wxautox4 import WeChat
    try:
        wx = WeChat(version="微信")
    except Exception:
        wx = WeChat(version="WeChat")
    w(f"WeChat 初始化完成，HWND={getattr(wx, 'HWND', None)}")

    if args.resize:
        _resize_main(args.resize)
    load_procs = _spawn_load(args.load) if args.load else []
    if load_procs:
        time.sleep(2)
        w(f"已起 {len(load_procs)} 个负载子进程，CPU={psutil.cpu_percent(interval=1)}%")

    ok_n = 0
    odd_fail = 0      # 连续出现"不是目标现象"的失败（如 EditControl 找不到），说明微信状态不对，停
    aborted = None
    try:
        for i in range(1, args.n + 1):
            before = _wx_top_windows()
            if not _logged_in(before):
                aborted = f"第 {i} 轮开始前微信主窗口不在登录态，停止实验（窗口: {_wx_titles(before)}）"
                w("!! " + aborted)
                break
            env = _env_snapshot()
            CALLS.clear()
            _T0[0] = time.perf_counter()
            err = None
            t0 = time.time()
            try:
                wx.AddListenChat(nickname=args.target, callback=lambda m: None)
            except Exception as e:
                err = repr(e)
            cost = round(time.time() - t0, 2)
            calls = list(CALLS)
            sub = None
            try:
                sub = wx.GetSubWindow(nickname=args.target)
            except Exception as e:
                if err is None:
                    err = f"GetSubWindow: {e!r}"
            ok = err is None and sub is not None
            after = _wx_top_windows()
            cpu = psutil.cpu_percent(interval=None)
            shot = None
            if not ok:
                shot = os.path.join(DATA_DIR, f"diag-shot-{STAMP}-{i:02d}.png")
                if not _screenshot(shot):
                    shot = None
            try:
                wx.RemoveListenChat(args.target)
            except Exception as e:
                w(f"  清理监听失败: {e!r}")

            row = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "round": i, "ok": ok, "err": err,
                "cost_sec": cost, "cpu_pct": cpu, "load_procs": len(load_procs),
                "wx_before": _wx_titles(before), "wx_after": _wx_titles(after),
                "main_rect": env.get("wx_main_rect"), "fg": env.get("fg_title"),
                "rustdesk_conns": env.get("rustdesk_conns"), "calls": calls, "shot": shot,
            }
            with open(OUT_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            ok_n += int(ok)
            mouse = [c for c in calls if c.get("msg") in
                     ("WM_LBUTTONDOWN", "WM_LBUTTONUP", "WM_LBUTTONDBLCLK", "WM_MOUSEMOVE")
                     or c["call"].endswith(("mouse_event", "SendInput", "SetCursorPos"))]
            w(f"[{i:02d}] {'OK ' if ok else 'BAD'} cost={cost}s cpu={cpu}% "
              f"窗口 {len(before or [])}->{len(after or [])} 底层调用 {len(calls)} 条（鼠标类 {len(mouse)}）"
              f"{'  ' + err if err else ''}")
            for c in mouse[:12]:
                w(f"      t+{c['t']:.3f}s {c['call']:<24} {c.get('msg', '')} hwnd={c.get('hwnd')} "
                  f"x={c.get('x')} y={c.get('y')}")
            if len(mouse) > 12:
                w(f"      ... 共 {len(mouse)} 条")
            if not _logged_in(after):
                aborted = f"第 {i} 轮之后微信主窗口不在登录态，停止实验（窗口: {_wx_titles(after)}）"
                w("!! " + aborted)
                break
            if ok or (err and "MoveWindow" in err):
                odd_fail = 0
            else:
                odd_fail += 1
                if odd_fail >= 3:
                    aborted = f"连续 {odd_fail} 次非目标错误（{err}），微信状态不对，停止实验"
                    w("!! " + aborted)
                    break
            time.sleep(args.interval)
    finally:
        for p in load_procs:
            try:
                p.kill()
            except Exception:
                pass

    w(f"# 完成：{ok_n}/{args.n} 成功。逐轮数据 {OUT_JSONL}")
    w("# 读法：失败轮的 calls 里若一条鼠标类调用都没有 → 点击走的通道没被打桩到（换通道再挂）；"
      "若有且坐标/hwnd 与成功轮一致 → 微信收到了同样的消息却没建窗，问题在微信侧（时序/状态）；"
      "若坐标或 hwnd 不同 → wxautox 找错了会话项/窗口。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    ap.add_argument("--n", type=int, default=30, help="循环次数")
    ap.add_argument("--interval", type=float, default=5.0, help="每轮间隔秒")
    ap.add_argument("--target", default="文件传输助手", help="靶子会话")
    ap.add_argument("--load", type=int, default=0, help="起 K 个吃 CPU 的子进程做 A/B")
    ap.add_argument("--resize", default="", help="先把主窗口调成 WxH，如 1500x1048 / 1040x736")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
