# -*- coding: utf-8 -*-
"""
长期探针：定时对一个固定会话调 `AddListenChat`，把成败和当时的环境记成 JSONL，
攒够失败样本再回头找规律。

为什么需要它：真实失败率约 5%，而失败只在真人发消息时才暴露，一天撞不上几次。
手工诊断连跑 18 轮一次没复现，靠人守着等不来。探针每 10 分钟自动打一次，
一天 144 个样本，几天就能看出分布。

★ 必须跑在 bot 进程内、由 schedule 主循环串行调用。
  独立进程/线程做微信 UI 操作会和主循环抢窗口（CLAUDE.md 3.11 血泪：
  18:44 抢焦点导致粘贴打在主窗口上，连败 3 次）。

靶子默认用「文件传输助手」：系统会话，开关它的独立窗口不打扰真人、不产生已读回执。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from .config import DATA_DIR, load

try:
    from logger import log as _log
except Exception:
    def _log(level="INFO", message=""):
        print(f"[{level}] {message}")


def log(level: str, message: str) -> None:
    # 单测不许写进生产日志：本文件的用例会打出「触发 SWXPanelRestart 自愈重启」
    # 这类以假乱真的行，混进 panel_logs 后排查真故障时根本分不出真假（2026-08-15 踩到）。
    if os.environ.get("NCC_LOG_SILENT") == "1":
        print(f"[{level}] [listen_health] {message}")
        return
    try:
        _log(level=level, message=f"[listen_health] {message}")
    except Exception:
        pass


_consecutive_fail = 0


def _visible_chat_windows():
    """当前微信可见聊天窗口数；拿不到（非 Windows / 缺依赖）返回 None。"""
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return None
    count = [0]

    def cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ('weixin.exe', 'wechat.exe'):
                return
        except Exception:
            return
        try:
            if win32gui.GetClassName(hwnd) == 'Qt51514QWindowIcon' and win32gui.IsWindowVisible(hwnd):
                count[0] += 1
        except Exception:
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None
    return count[0]


def _wx_top_windows():
    """微信全部顶层窗口，按 Z 序（EnumWindows 就是从上往下枚举），**含不可见的**。

    为什么要含不可见：原来的 `_visible_chat_windows()` 只数可见的，于是「窗口建出来了
    但没显示」这种情况会被误判成「一个都没建」。08-12 的窗口清单取证表明确实不存在
    目标窗口，但那是单次快照；这里每轮都记，把这个盲点彻底关掉。
    """
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return None
    out = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            name = psutil.Process(pid).name().lower()
            if name not in ('weixin.exe', 'wechat.exe', 'wechatappex.exe'):
                return
        except Exception:
            return
        try:
            out.append({
                'h': h,
                'cls': win32gui.GetClassName(h),
                't': win32gui.GetWindowText(h),
                'v': int(bool(win32gui.IsWindowVisible(h))),
                'r': list(win32gui.GetWindowRect(h)),
                'p': name,
            })
        except Exception:
            return

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None
    return out


def _hit_test_session_list(main_hwnd):
    """会话列表那几个点上，**实际压在最上面的是哪个窗口**。

    这是本轮调查留下的头号待验假设的直接判据。08-12 实测：尝试那一刻前台是 Chrome
    面板的 70 个样本 0 失败，是微信某个窗口的 32 个样本 28 失败（87.5%）。最可疑的机制
    是——微信已在前台时 wxautox 跳过了「激活主窗口」，而真正盖在会话列表上的是别的窗口，
    于是双击打空、微信没收到建窗请求、FindWindow 空等满 3 秒死线。

    WindowFromPoint 是纯只读的，不移动鼠标、不点击、不抢焦点，不违反「独立进程不得做
    微信 UI 操作」那条铁律（何况这里本来就跑在 bot 进程内）。
    """
    if not main_hwnd:
        return None
    try:
        import ctypes
        import win32gui
        import win32process
        import psutil
    except Exception:
        return None
    try:
        l, t, r, b = win32gui.GetWindowRect(main_hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
    except Exception:
        return None

    class _PT(ctypes.Structure):
        _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

    u32 = ctypes.windll.user32
    u32.WindowFromPoint.argtypes = [_PT]
    u32.WindowFromPoint.restype = ctypes.c_void_p

    hits = []
    # 左侧会话列表那一列，纵向取三个点（会话行高约 60px，取 15%/30%/45% 高度处）
    for fx, fy in ((0.15, 0.15), (0.15, 0.30), (0.15, 0.45)):
        x, y = int(l + w * fx), int(t + h * fy)
        try:
            hh = u32.WindowFromPoint(_PT(x, y))
            hh = int(hh) if hh else 0
        except Exception:
            hits.append({'pt': [x, y], 'e': 'WindowFromPoint failed'})
            continue
        item = {'pt': [x, y], 'h': hh}
        try:
            item['cls'] = win32gui.GetClassName(hh)
            item['t'] = win32gui.GetWindowText(hh)
            root = win32gui.GetAncestor(hh, 2)      # GA_ROOT：落点所属的顶层窗口
            item['root'] = root
            item['is_main'] = int(root == main_hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hh)
            item['proc'] = psutil.Process(pid).name()
        except Exception:
            pass
        hits.append(item)
    return hits


def _gui_resources():
    """每个微信进程的 GDI/USER 对象数与**峰值**。

    句柄配额假设这轮已被证伪（Weixin GDI 实测 39/10000，且 CreateWindowEx 消耗的是
    USER + 桌面堆、不是 GDI 句柄）。这里仍留一份，只为把它永久封棺：峰值由 win32k 维护、
    进程生命周期内单调不减，所以哪怕采不到失败那一瞬间，跨过一整个发作块后峰值仍然低，
    就能回溯覆盖这个进程经历过的每一次发作。跨会话取不到（恒返回 0 + GetLastError=87），
    只有跑在会话 2 内的本进程能拿到 —— 所以这几个字段顺带还是「探针确实在会话 2」的自检。
    """
    try:
        import ctypes
        import psutil
    except Exception:
        return None
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u32.GetGuiResources.restype = ctypes.c_uint
    k32.OpenProcess.restype = ctypes.c_void_p
    out = []
    for p in psutil.process_iter(['name', 'pid']):
        try:
            nm = (p.info.get('name') or '').lower()
            if nm not in ('weixin.exe', 'wechat.exe', 'wechatappex.exe'):
                continue
            row = {'pid': p.info['pid'], 'name': p.info['name']}
            hp = k32.OpenProcess(0x0400, False, p.info['pid'])   # QUERY_INFORMATION
            if not hp:
                row['e'] = 'open:%d' % ctypes.get_last_error() if hasattr(ctypes, 'get_last_error') else 'open'
                out.append(row)
                continue
            try:
                for key, flag in (('gdi', 0), ('user', 1), ('gdi_peak', 2), ('user_peak', 4)):
                    row[key] = int(u32.GetGuiResources(hp, flag))
                if row.get('gdi') == 0 and row.get('user') == 0:
                    # 全 0 基本等于跨会话调用失败 —— 记一笔，别把 0 当真值去下结论
                    row['e'] = 'all-zero(可能不在会话2)'
            finally:
                k32.CloseHandle(hp)
            try:
                row['handles'] = p.num_handles()
                row['threads'] = p.num_threads()
                row['ws_mb'] = int(p.memory_info().rss / 1048576)
            except Exception:
                pass
            out.append(row)
        except Exception:
            continue
    return out or None


def _env_snapshot() -> dict:
    """
    采样时的环境快照。wxautox 作者说这类「窗口丢失」多半是被人为/后台软件干扰，
    所以把「坏掉那一刻谁在前台、有没有人远程连着、微信主窗口是哪个句柄」记下来，
    等真出故障时才有得对。全程吞异常，非 Windows 返回空。
    """
    snap = {}
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        return snap

    # 前台窗口是谁——被别的程序抢了焦点，双击就可能落空。
    # 08-12 复盘发现这是目前最强的判别式，但光有 title/proc 分不清「主窗口在前台」和
    # 「某个聊天子窗口在前台」（前者当天 15/15 全败、后者 16/21），所以补上 hwnd 和类名。
    try:
        hwnd = win32gui.GetForegroundWindow()
        snap['fg_title'] = win32gui.GetWindowText(hwnd)
        snap['fg_hwnd'] = hwnd
        try:
            snap['fg_class'] = win32gui.GetClassName(hwnd)
        except Exception:
            snap['fg_class'] = None
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            snap['fg_proc'] = psutil.Process(pid).name()
        except Exception:
            snap['fg_proc'] = None
    except Exception:
        pass

    # 有没有人正连着远程（RustDesk console 模式不写 TerminalServices 事件日志，
    # 只能这样看）。ESTABLISHED 连接数 > 0 基本等于「有人在看着这台机器」。
    try:
        est = 0
        for p in psutil.process_iter(['name']):
            if 'rustdesk' not in (p.info.get('name') or '').lower():
                continue
            try:
                est += sum(1 for c in p.net_connections(kind='inet')
                           if c.status == psutil.CONN_ESTABLISHED)
            except Exception:
                continue
        snap['rustdesk_conns'] = est
    except Exception:
        pass

    # 微信主窗口句柄。08-04 实测它会在进程不重启的情况下被销毁重建
    # （264072 -> 64685408），而 wxautox 启动时是把它缓存下来用的。
    try:
        found = []

        def cb(h, _):
            try:
                if (win32gui.GetClassName(h) == 'Qt51514QWindowIcon'
                        and win32gui.IsWindowVisible(h)
                        and win32gui.GetWindowText(h) in ('微信', 'Weixin', 'WeChat')):
                    found.append(h)
            except Exception:
                return
        win32gui.EnumWindows(cb, None)
        snap['wx_main_hwnd'] = found[0] if found else None
    except Exception:
        pass

    # 主窗口自己的几何/显示状态。「置前=True 但仍然开不出窗口」是日报日志里反复出现的
    # 矛盾（08-12 四次失败全是这样），所以把最小化、rect、可见性都记下来。
    try:
        mh = snap.get('wx_main_hwnd')
        if mh:
            snap['wx_main_rect'] = list(win32gui.GetWindowRect(mh))
            snap['wx_main_iconic'] = int(bool(win32gui.IsIconic(mh)))
            snap['wx_main_visible'] = int(bool(win32gui.IsWindowVisible(mh)))
            snap['wx_main_is_fg'] = int(mh == snap.get('fg_hwnd'))
    except Exception:
        pass

    # 下面三块是本轮新加的取证埋点，全部只读；任何一块挂掉都只是缺字段，不影响探针判定。
    try:
        snap['wx_windows'] = _wx_top_windows()
    except Exception:
        pass
    try:
        snap['hit_test'] = _hit_test_session_list(snap.get('wx_main_hwnd'))
    except Exception:
        pass
    try:
        snap['gui'] = _gui_resources()
    except Exception:
        pass

    return snap


def _record(row: dict) -> None:
    """一行 JSON 落盘。写失败只记日志，绝不往上抛。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"probe-{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log("WARNING", f"探针写盘失败：{e}")


def _uptime_hours(bot):
    try:
        return round((datetime.now() - bot.start_time).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def tick(bot) -> None:
    """跑一轮探针。任何异常都不能冒泡到 schedule 主循环。"""
    global _consecutive_fail
    try:
        cfg = load()
        pcfg = cfg.get('probe', {})
        if not pcfg.get('enabled', True):
            return
        target = pcfg.get('target') or '文件传输助手'
        wx = getattr(bot, 'wx', None)
        if wx is None:
            return

        # 目标万一已经是被正式监听的会话，跳过本轮，别去动人家的窗口
        try:
            existing = [getattr(s, 'who', s) for s in (wx.GetAllSubWindow() or [])]
            if target in [str(x) for x in existing]:
                log("INFO", f"探针目标 {target} 已在监听中，跳过本轮")
                return
        except Exception:
            existing = None

        win_before = _visible_chat_windows()
        snapshot = _env_snapshot()
        err = None
        t0 = time.time()
        try:
            wx.AddListenChat(nickname=target, callback=lambda msg: None)
        except Exception as e:
            err = repr(e)
        cost = round(time.time() - t0, 2)

        sub = None
        try:
            sub = wx.GetSubWindow(nickname=target)
        except Exception as e:
            if err is None:
                err = f"GetSubWindow: {e!r}"
        ok = err is None and sub is not None
        win_after = _visible_chat_windows()

        # 失败时再取一份**只读**快照：窗口清单/双击落点/前台在这 3.4 秒里到底变了没有。
        # 故意不做「隔 2s/10s 再 AddListenChat 复测一次」——那等于每次失败多打两次微信 UI，
        # 而「bot 驱动 UI 的强度」正是本轮头号放大器嫌疑，不能拿采样去污染被测对象。
        snapshot_after = None
        if not ok:
            try:
                snapshot_after = _env_snapshot()
            except Exception:
                pass

        # 清理，别让探针窗口累积
        try:
            wx.RemoveListenChat(target)
        except Exception as e:
            log("WARNING", f"探针清理监听失败：{e}")

        _record({
            "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "target": target,
            "ok": ok,
            "err": err,
            "cost_sec": cost,
            "win_before": win_before,
            "win_after": win_after,
            "subwin_count": (len(existing) if isinstance(existing, list) else None),
            "bot_uptime_h": _uptime_hours(bot),
            "dyn_listen_count": len(getattr(bot, 'all_Mode_listen_list', []) or []),
            "msg_received": getattr(bot, 'msg_received_count', None),
            "env": snapshot,
            "env_after": snapshot_after,
        })

        if ok:
            if _consecutive_fail:
                log("INFO", f"探针恢复正常（此前连续失败 {_consecutive_fail} 次）")
            _consecutive_fail = 0
        else:
            _consecutive_fail += 1
            log("WARNING", f"探针第 {_consecutive_fail} 次连续失败：{err} 环境={snapshot}")
            threshold = int(pcfg.get('alert_after_consecutive', 3))
            if _consecutive_fail == threshold:
                try:
                    from .alert import alert_listen_failure
                    alert_listen_failure(
                        bot, f"[探针] {target}",
                        retry_count=_consecutive_fail, last_error=err,
                        window_count=win_before,
                    )
                except Exception as e:
                    log("WARNING", f"探针告警失败：{e}")
            # 自愈：连续失败说明进入了「窗口丢失」坏状态，重启程序即可恢复（见 heal.py）
            try:
                from .heal import maybe_heal
                maybe_heal(bot, _consecutive_fail, pcfg, last_error=err, snapshot=snapshot)
            except Exception as e:
                log("WARNING", f"自愈调用失败：{e}")
    except Exception as e:
        log("ERROR", f"探针本轮出错（已吞掉）：{e}")


def register(bot, schedule) -> None:
    """挂进 bot 的 schedule。由 wxbot_core 在定时任务注册处调用。

    间隔取自配置，改间隔要重启机器人线程才生效（schedule 注册时就固定了）。
    """
    cfg = load()
    pcfg = cfg.get('probe', {})
    if not pcfg.get('enabled', True):
        log("INFO", "探针已关闭，不挂载")
        return
    interval = max(1, int(pcfg.get('interval_min', 10)))
    schedule.clear("listen_probe")
    schedule.every(interval).minutes.do(tick, bot).tag("listen_probe")
    # 主循环只在有定时任务开关打开时才 run_pending，这个标志让它把我们也算上
    bot._listen_probe_enabled = True
    log("INFO", f"监听探针已挂载：每 {interval} 分钟对「{pcfg.get('target') or '文件传输助手'}」打一次")
