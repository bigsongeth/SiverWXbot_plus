# -*- coding: utf-8 -*-
"""
diag_open_window.py —— 把「AddListenChat 弹不出独立窗口（MoveWindow 1400）」这个黑盒打透明。

★ 必须在会话 2 跑（SWXRun），且机器人已停（面板 /stop_bot）。两个进程抢同一个微信 = 结果作废。
★ 不碰 wxautox 源码（都是 .pyd），只在【它调用的底层模块】上打桩（打桩实现在 tap.py，和生产共用）。

背景（2026-09-02 复盘 1154 个探针样本 + 8 月全部日志得出的结论，见 CLAUDE.md 3.18）：
  - 失败 = 微信压根没建出新窗口（顶层窗口数前后不变），不是"建了但没找到"。
  - 已证伪：前台是谁 / 会话列表被谁盖住 / 别的线程并发操作 UI / 「找到当前会话」分支 /
    重启能治 / CPU 负载 / 主窗口尺寸（09-03 凌晨独立进程 24/24 全成功）。
  - 41.x 的双击是 `uiplug.Win32.click_by_bbox(double_click=True)` 用 `win32api.SendMessage`
    往主窗口句柄直送 5 条消息：按下/抬起、按下/抬起、WM_LBUTTONDBLCLK。
  - 2026-09-06 14:59 生产录像（tap.py）：4 次失败发的 5 条消息与成功时逐字节相同（同句柄、同坐标），
    区别只在微信处理那条 DBLCLK 花的时间：成功 0.38–0.70s（在建窗），失败 0.15–0.21s（没建）。
    → 问题在微信怎么解读这组假点击。SendMessage 不进队列、Qt 给它的时间戳取自
    GetMessageTime()（该线程上一条从队列取出的消息的时间），而 Qt 判双击靠两次按下的时间戳差。

2026-09-06 16:31 定案（全部实验结果见 CLAUDE.md 3.18「根因钉死」）：病因是 wxautox 的 4 个监听线程
并发往不同子窗口发假点击，交错后 Qt 进程级鼠标状态卡住；一次真实鼠标单击可复位；
`LISTENER_EXCUTOR_WORKERS=1` 可预防（挂 5 个监听 8/8）。灌水 WM_NULL / PostMessage / 时间戳那一层是弯路。

本脚本能做的实验：
  --listen @文件     先像生产一样挂上一批监听再测（一行一个名字，UTF-8）——这是能复现故障的那一组
  --listen-workers N 改 WxParam.LISTENER_EXCUTOR_WORKERS（1 = 预防修法）
  --listen-interval N 改 WxParam.LISTEN_INTERVAL（已证伪：与轮询频率无关）
  --unstick MODE     循环前做一次复位：click（有效）/ minmax / activate（无效）/ cancelmode（无效）/ esc（别用，收托盘）
  --flood MS         每 MS 毫秒往主窗口 PostMessage 一条 WM_NULL（已证伪：灌水本身不致病）
  --post             wxautox 的 SendMessage 鼠标消息改 PostMessage（已证伪）
  --realclick        拦下假点击改真实鼠标双击（坏状态下也开不出）
  --load K / --resize WxH   更早的 A/B（已证伪）

跑法（会话 2）：payload 改成
      set PYTHONIOENCODING=utf-8
      cd /d C:\\Users\\Admin\\SiverWXbot_plus-main
      python plugins\\listen_health\\diag_open_window.py --n 8 --interval 15 --flood 20
  然后 schtasks /run /tn SWXRun。建议顺序：--flood 20 → --flood 20 --post → 空载对照。

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
import threading
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from plugins.listen_health.config import DATA_DIR          # noqa: E402
from plugins.listen_health.probe import _wx_top_windows, _env_snapshot  # noqa: E402
from plugins.listen_health import tap as T                  # noqa: E402

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
OUT_JSONL = os.path.join(DATA_DIR, f"diag-{STAMP}.jsonl")
OUT_LOG = os.path.join(DATA_DIR, f"diag-{STAMP}.log")
CLICK_NAMES = ("WM_LBUTTONDOWN", "WM_LBUTTONUP", "WM_LBUTTONDBLCLK")


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


class Flood:
    """实验期间往主窗口队列里灌 WM_NULL。用打桩前存下的原始 PostMessage，不会混进录制。"""

    def __init__(self, hwnd: int, interval_ms: int):
        self.hwnd = hwnd
        self.interval = max(1, int(interval_ms)) / 1000.0
        self._stop = threading.Event()
        self.sent = 0
        self._thread = threading.Thread(target=self._run, name="wm_null_flood", daemon=True)

    def _run(self):
        post = T._ORIG_POST.get("win32api")
        if post is None:
            import win32api
            post = win32api.PostMessage
        while not self._stop.is_set():
            try:
                post(self.hwnd, 0x0000, 0, 0)   # WM_NULL
                self.sent += 1
            except Exception:
                pass
            time.sleep(self.interval)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()


def _wx_titles(wins):
    return T._wx_titles(wins)


def _unstick(mode: str, hwnd: int) -> str:
    """把微信从"再也开不出独立窗口"的坏状态里拉回来的候选动作（2026-09-06 16:13 实测：
    监听轮询把它搞坏之后，哪怕轮询全停、UI 线程完全空闲，双击照样不算数）。"""
    import win32api
    import win32con
    import win32gui
    if mode == "esc":
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
        return "PostMessage Esc 到主窗口"
    if mode == "click":
        sx, sy = win32gui.ClientToScreen(hwnd, (600, 900))     # 聊天区空白处，避开会话列表和输入框
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return f"真实鼠标单击主窗口空白处 ({sx},{sy})"
    if mode == "minmax":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(1)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1)
        return "最小化再还原主窗口"
    if mode == "cancelmode":
        win32api.SendMessage(hwnd, 0x001F, 0, 0)     # WM_CANCELMODE
        return "SendMessage WM_CANCELMODE"
    if mode == "activate":
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.5)
        return "SwitchToThisWindow 激活主窗口（wxautox 初始化里的 _show）"
    return f"不认识的 unstick 模式 {mode!r}"


class RealClick:
    """--realclick：拦下 wxautox 那 5 条假点击消息，改用真实鼠标输入在同一个位置双击。

    第一条 WM_LBUTTONDOWN 到来时：把它的客户区坐标换算成屏幕坐标，SetCursorPos 过去，
    mouse_event 按下/抬起两次（间隔 80ms）；随后 4 条消息直接吞掉。
    用来判定：灌水状态下微信认不认【真】双击。认 → 问题只在假消息的解读；不认 → 微信整条双击链路被堵。
    """

    def __init__(self):
        self.count = 0
        self.done = 0

    def __call__(self, args) -> bool:
        import win32api
        import win32con
        import win32gui
        self.count += 1
        if self.count % 5 != 1:          # 只在每组 5 条的第一条上动手，其余吞掉
            return True
        hwnd, _msg, _wp, lp = args[0], args[1], args[2], args[3]
        lp &= 0xFFFFFFFF
        x, y = lp & 0xFFFF, (lp >> 16) & 0xFFFF
        sx, sy = win32gui.ClientToScreen(hwnd, (x, y))
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.05)
        for _ in range(2):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.03)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.08)
        self.done += 1
        return True


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

    wins0 = _wx_top_windows() or []
    if not any(x.get("cls") == "Qt51514QWindowIcon" for x in wins0):
        w("!! 枚举不到任何微信顶层窗口 —— 多半没在会话 2（SSH 是 session 0），停。")
        return 2
    w(f"微信顶层窗口: {_wx_titles(wins0)}")
    if not _logged_in(wins0):
        w("!! 微信主窗口不在登录态（没有 ≥600x500 的可见主窗口），不开始。")
        return 3

    import psutil
    psutil.cpu_percent(interval=None)
    w(f"DoubleClickTime={ctypes.windll.user32.GetDoubleClickTime()} ms  "
      f"屏幕={ctypes.windll.user32.GetSystemMetrics(0)}x{ctypes.windll.user32.GetSystemMetrics(1)}")

    taps = T.install_taps()
    w(f"打桩成功: {taps}")
    T._POST_CLICKS[0] = bool(args.post)
    if args.post:
        w("实验开关：鼠标消息改走 PostMessage")
    realclick = None
    if args.realclick:
        realclick = RealClick()
        T._INTERCEPT[0] = realclick
        w("实验开关：拦下假点击，改用真实鼠标在同一位置双击")

    from wxautox4 import WeChat, WxParam
    if args.listen_interval:
        WxParam.LISTEN_INTERVAL = int(args.listen_interval)
        w(f"WxParam.LISTEN_INTERVAL 改为 {WxParam.LISTEN_INTERVAL}s")
    if args.listen_workers:
        WxParam.LISTENER_EXCUTOR_WORKERS = int(args.listen_workers)
        w(f"WxParam.LISTENER_EXCUTOR_WORKERS 改为 {WxParam.LISTENER_EXCUTOR_WORKERS}")
    try:
        wx = WeChat(version="微信")
    except Exception:
        wx = WeChat(version="WeChat")
    main_hwnd = _main_hwnd()
    w(f"WeChat 初始化完成，主窗口 hwnd={main_hwnd}")

    # --listen：先像生产一样挂上一批监听（各自开独立窗口 + wxautox 监听线程轮询），再测靶子。
    # 用来判定"生产里往微信 UI 线程灌水的是不是 wxautox 自己的监听轮询"。
    listened = []
    listen_spec = args.listen or ""
    if listen_spec.startswith("@"):        # .cmd 里写不了中文/emoji，名字从 UTF-8 文件读，一行一个
        with open(listen_spec[1:], "r", encoding="utf-8-sig") as f:
            listen_spec = "|".join(line.strip() for line in f if line.strip())
    for name in [s.strip() for s in listen_spec.split("|") if s.strip()]:
        try:
            r = wx.AddListenChat(nickname=name, callback=lambda m: None)
            ok_l = bool(r) and wx.GetSubWindow(nickname=name) is not None
        except Exception as e:
            ok_l = False
            w(f"  预挂监听 {name} 异常: {e!r}")
        w(f"  预挂监听 {name}: {'OK' if ok_l else 'BAD'}")
        if ok_l:
            listened.append(name)
    if listened:
        time.sleep(3)
        w(f"已挂 {len(listened)} 个监听，等 3 秒让轮询线程跑起来")
    if args.unstick:
        try:
            w(f"复位动作 --unstick {args.unstick}: {_unstick(args.unstick, main_hwnd)}")
        except Exception as e:
            w(f"复位动作 {args.unstick} 异常: {e!r}")
        time.sleep(1)

    if args.resize:
        _resize_main(args.resize)
    load_procs = _spawn_load(args.load) if args.load else []
    if load_procs:
        time.sleep(2)
        w(f"已起 {len(load_procs)} 个负载子进程，CPU={psutil.cpu_percent(interval=1)}%")
    flood = None
    if args.flood:
        if not main_hwnd:
            w("!! 找不到主窗口句柄，--flood 无法执行")
            return 4
        flood = Flood(main_hwnd, args.flood)
        flood.start()
        w(f"WM_NULL 灌水线程已起，每 {args.flood}ms 一条")

    ok_n = 0
    odd_fail = 0
    aborted = None
    try:
        for i in range(1, args.n + 1):
            before = _wx_top_windows()
            if not _logged_in(before):
                aborted = f"第 {i} 轮开始前微信主窗口不在登录态，停止实验（窗口: {_wx_titles(before)}）"
                w("!! " + aborted)
                break
            env = _env_snapshot()
            T.CALLS.clear()
            T._T0[0] = time.perf_counter()
            T._ACTIVE[0] = True
            err = None
            t0 = time.time()
            try:
                wx.AddListenChat(nickname=args.target, callback=lambda m: None)
            except Exception as e:
                err = repr(e)
            finally:
                T._ACTIVE[0] = False
            cost = round(time.time() - t0, 2)
            calls = list(T.CALLS)
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
                "flood_ms": args.flood, "flood_sent": (flood.sent if flood else 0), "post": bool(args.post),
                "wx_before": _wx_titles(before), "wx_after": _wx_titles(after),
                "main_rect": env.get("wx_main_rect"), "fg": env.get("fg_title"),
                "rustdesk_conns": env.get("rustdesk_conns"), "calls": calls, "shot": shot,
            }
            with open(OUT_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            ok_n += int(ok)
            mouse = [c for c in calls if c.get("msg") in CLICK_NAMES
                     or c["call"].endswith(("mouse_event", "SendInput", "SetCursorPos"))]
            dbl_cost = ""
            if len(mouse) >= 5 and mouse[4].get("msg") == "WM_LBUTTONDBLCLK":
                dbl_cost = f" 双击处理耗时={mouse[4]['t'] - mouse[3]['t']:.2f}s"
            w(f"[{i:02d}] {'OK ' if ok else 'BAD'} cost={cost}s cpu={cpu}% "
              f"窗口 {len(before or [])}->{len(after or [])} 底层调用 {len(calls)} 条（鼠标类 {len(mouse)}）{dbl_cost}"
              f"{'  ' + err if err else ''}")
            for c in mouse[:12]:
                w(f"      t+{c['t']:.3f}s {c['call']:<32} {c.get('msg', '')} hwnd={c.get('hwnd')} "
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
        if flood:
            flood.stop()
        T._POST_CLICKS[0] = False
        T._INTERCEPT[0] = None
        if realclick:
            w(f"真实双击共执行 {realclick.done} 次")
        for name in listened:
            try:
                wx.RemoveListenChat(name)
            except Exception as e:
                w(f"  撤预挂监听 {name} 失败: {e!r}")
        for p in load_procs:
            try:
                p.kill()
            except Exception:
                pass

    w(f"# 完成：{ok_n}/{args.n} 成功"
      f"{'（灌水共 %d 条）' % flood.sent if flood else ''}。逐轮数据 {OUT_JSONL}")
    w("# 读法：失败轮的 calls 里若一条鼠标类调用都没有 → 点击走的通道没被打桩到（换通道再挂）；"
      "若有且坐标/hwnd 与成功轮一致 → 微信收到了同样的消息却没建窗，看双击处理耗时（建窗≈0.4–0.7s，没建≈0.15s）；"
      "若坐标或 hwnd 不同 → wxautox 找错了会话项/窗口。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 2)[1])
    ap.add_argument("--n", type=int, default=30, help="循环次数")
    ap.add_argument("--interval", type=float, default=5.0, help="每轮间隔秒")
    ap.add_argument("--target", default="文件传输助手", help="靶子会话")
    ap.add_argument("--load", type=int, default=0, help="起 K 个吃 CPU 的子进程做 A/B")
    ap.add_argument("--resize", default="", help="先把主窗口调成 WxH，如 1500x1048 / 1040x736")
    ap.add_argument("--flood", type=int, default=0, help="实验期间每 N 毫秒往主窗口 PostMessage 一条 WM_NULL")
    ap.add_argument("--post", action="store_true", help="把 wxautox 的 SendMessage 鼠标消息改成 PostMessage")
    ap.add_argument("--realclick", action="store_true", help="拦下假点击，改用真实鼠标输入在同一位置双击")
    ap.add_argument("--listen", default="", help="先挂上这些监听再测，用 | 分隔，如 '松爸|肥肉测试1🐶'")
    ap.add_argument("--listen-interval", type=int, default=0, help="改 WxParam.LISTEN_INTERVAL（秒），0=不改")
    ap.add_argument("--unstick", default="", help="循环前做一次复位动作：esc / click / minmax / cancelmode / activate")
    ap.add_argument("--listen-workers", type=int, default=0, help="改 WxParam.LISTENER_EXCUTOR_WORKERS（监听线程数），0=不改")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
