# -*- coding: utf-8 -*-
"""每日 AI 日报 -> 微信收藏笔记 -> 发送到目标群/人。

整条链路（全部实测通过，微信 4.1.9 / wxautox4 内置 uiautomation）：
  render -> CF_HTML 剪贴板 -> 新建收藏笔记(粘贴) -> 关闭存收藏
  -> 搜索打开目标(校验防发错) -> 点"发送收藏" -> 选中该笔记 -> 点"发送"

注意：本模块在 bot 进程内、由 schedule 主循环单线程调用，和 bot 其它 UI 操作串行，
不会和 bot 抢微信 UI。
"""
import os
import time
import json
import datetime

from wxautox4.uia import uiautomation as auto
import win32clipboard as wc
import win32gui
import win32con
import win32api
import win32process

from . import config
from .render import render, render_plain

_LOG = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "ai_news_note.log")
_STATE = os.path.join(os.path.dirname(__file__), "last_sent.txt")


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


# ---------------- 剪贴板 ----------------
def _build_cf_html(frag):
    pre = "<html><body>\r\n<!--StartFragment-->"
    post = "<!--EndFragment-->\r\n</body></html>"
    hdr = ("Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
           "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n")
    sh = len(hdr.format(0, 0, 0, 0).encode())
    sf = sh + len(pre.encode())
    ef = sf + len(frag.encode())
    eh = ef + len(post.encode())
    return (hdr.format(sh, eh, sf, ef) + pre + frag + post).encode("utf-8")


def _clip_html(cf_bytes, plain):
    CF = wc.RegisterClipboardFormat("HTML Format")
    wc.OpenClipboard(); wc.EmptyClipboard()
    wc.SetClipboardData(CF, cf_bytes)
    wc.SetClipboardData(wc.CF_UNICODETEXT, plain)
    wc.CloseClipboard()


def _clip_text(t):
    wc.OpenClipboard(); wc.EmptyClipboard()
    wc.SetClipboardData(wc.CF_UNICODETEXT, t)
    wc.CloseClipboard()


def _clip_read():
    for _ in range(5):
        try:
            wc.OpenClipboard()
            d = wc.GetClipboardData(wc.CF_UNICODETEXT)
            wc.CloseClipboard()
            return d
        except Exception:
            time.sleep(0.4)
    return None


# ---------------- UIA helpers ----------------
def _top(cls, name=None):
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ClassName == cls and (name is None or (w.Name or "") == name):
                return w
        except Exception:
            pass
    return None


def _find_in(win, pred, max_depth=27):
    hit = [None]
    def f(c, d):
        if hit[0] or d > max_depth:
            return
        for ch in c.GetChildren():
            try:
                if pred(ch):
                    hit[0] = ch; return
            except Exception:
                pass
            f(ch, d + 1)
    f(win, 0)
    return hit[0]


def _find_global(pred):
    """在所有顶层窗口里找（收藏选择器有时是独立顶层窗口，不在聊天窗口的子树里）。"""
    for w in list(auto.GetRootControl().GetChildren()):
        try:
            if pred(w):
                return w
            hit = _find_in(w, pred)
            if hit:
                return hit
        except Exception:
            pass
    return None


# ---------------- 窗口置前（关键：clicks 打到别的窗口上会静默失败） ----------------
# 教训：面板 Chrome 最大化 / 别的全屏窗口盖住微信时，Click() 按屏幕坐标点下去，
# 点到的是盖在上面的那个窗口，微信毫无反应 —— 搜索框没聚焦、Ctrl+V 进了别处，
# 于是"搜索打开目标"永远失败（现象就是"未确认打开目标"）。而且 SSH/计划任务这种
# 非前台进程调 SetForegroundWindow 会被系统拒绝（报 error(0)），光调它没用。
# 解法：先 HWND_TOPMOST 强制抬到最上层（不需要前台权限），再用 AttachThreadInput
# 绕过前台锁抢焦点；用完恢复 NOTOPMOST。
_TOPMOSTED = set()


def _hw(h):
    """把窗口句柄归一化成有符号 32 位，用于比较两个来源不同的 hwnd。

    2026-08-09 定位到的老 bug：uiautomation 的 NativeWindowHandle 在句柄高位为 1 时
    给的是**无符号**大整数（如 18446744072896907420），而 pywin32 的
    GetForegroundWindow() 给的是**有符号**值（-812644196）—— 两者是同一个窗口，
    但直接 `==` 永远不相等，于是被误判成"没抢到前台"而中止发送。

    这就是所谓"间歇性抢焦点故障"的真正根因：句柄由系统分配，高位是不是 1 全看运气，
    所以同一套代码时好时坏（8/3 早上连挂三次、当晚手动跑却一次过）。
    凡是拿 win32gui 返回值和 NativeWindowHandle 比较的地方，两边都要过这个函数。
    """
    try:
        h = int(h) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return h
    return h - 0x100000000 if h >= 0x80000000 else h


def _describe_hwnd(hwnd):
    """把一个窗口画成一行可读信息，专供"抢不到前台"时留证据用。

    2026-08-03 教训：那天连续三次卡在"抢不到前台焦点"（日志只有 `前台=False` 一行），
    事后完全无法定位是谁占着前台 —— 等复现完事后再查，环境已经恢复正常了。
    所以失败分支必须当场把前台窗口的 hwnd/类名/进程/标题/是否置顶写进日志。
    本函数只读不写，任何异常都吞掉，绝不能因为记日志把发送流程搞挂。"""
    if not hwnd:
        return "hwnd=0（取不到前台窗口，多半是锁屏/会话断开）"
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = "?"
    try:
        title = win32gui.GetWindowText(hwnd) or ""
    except Exception:
        title = "?"
    pname, pid = "?", 0
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        h = win32api.OpenProcess(0x0410, False, pid)   # QUERY_INFORMATION|VM_READ
        try:
            pname = os.path.basename(win32process.GetModuleFileNameEx(h, 0))
        finally:
            win32api.CloseHandle(h)
    except Exception:
        pass
    try:
        topmost = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)
    except Exception:
        topmost = None
    ucls = ""
    try:
        # 微信窗口 Win32 视角是 Qt51514QWindowIcon，UIA 视角才是 mmui::XXX —— 两个都记上，
        # 免得事后对不上号。笔记编辑器属于另一个进程 WeChatAppEx.exe，不是 Weixin.exe。
        c = auto.ControlFromHandle(hwnd)
        if c:
            ucls = f" uia={c.ClassName!r}/{(c.Name or '')[:30]!r}"
    except Exception:
        pass
    # 句柄一律按归一化后的值打印，否则同一个窗口在日志里会出现两种写法
    # （无符号 18446744072896907420 / 有符号 -812644196），对不上号。
    return (f"hwnd={_hw(hwnd)} class={cls!r} proc={pname}(pid={pid}) "
            f"title={title[:60]!r} topmost={topmost}{ucls}")


def _log_fg(prefix):
    """记录"此刻前台窗口是谁"。返回该 hwnd。"""
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        fg = 0
    log(f"{prefix} 前台窗口 -> {_describe_hwnd(fg)}")
    return fg


def _force_foreground(hwnd):
    try:
        fg = win32gui.GetForegroundWindow()
        if _hw(fg) == _hw(hwnd):
            return True
        tid_me = win32api.GetCurrentThreadId()
        tid_fg = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        attached = False
        if tid_fg and tid_fg != tid_me:
            try:
                win32process.AttachThreadInput(tid_me, tid_fg, True)
                attached = True
            except Exception:
                pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if attached:
            try:
                win32process.AttachThreadInput(tid_me, tid_fg, False)
            except Exception:
                pass
    except Exception:
        pass
    return _hw(win32gui.GetForegroundWindow()) == _hw(hwnd)


def _raise_hwnd(hwnd, tag=""):
    """把窗口抬到最上层并尽量抢到前台。返回是否拿到前台焦点（拿不到也还能点，
    因为已经 topmost 了，点下去那一下会顺带激活它）。

    tag 只用于日志，标明这次是在抢哪个窗口（主窗口/笔记编辑器/聊天窗口…）。
    每次抢失败都记一行"当时前台是谁"：连着三次的持有者如果是同一个窗口，
    说明有别的程序稳稳占着前台；如果每次都不一样，说明在跟什么东西抢。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        _TOPMOSTED.add(hwnd)
    except Exception as e:
        log(f"置顶失败 hwnd={hwnd}: {e!r}")
    ok = False
    label = f"[{tag}]" if tag else ""
    for i in range(3):
        if _force_foreground(hwnd):
            ok = True
            break
        # 只在失败时记，正常路径不产生额外日志
        try:
            log(f"抢前台失败{label} 第 {i + 1}/3 次（目标 hwnd={hwnd}），"
                f"被占着 -> {_describe_hwnd(win32gui.GetForegroundWindow())}")
        except Exception:
            pass
        time.sleep(0.4)
    time.sleep(0.5)
    return ok


def _raise(win, tag=""):
    """比 _raise_hwnd 多一步 uiautomation 的 SetActive —— 非前台进程调裸的
    SetForegroundWindow 常被系统拒（拒绝访问），SetActive 里那套激活手法更管用。"""
    try:
        win.SetActive()
    except Exception:
        pass
    return _raise_hwnd(win.NativeWindowHandle, tag)


def _drop_topmost():
    """收尾：把这次抬过的窗口恢复成非置顶，别把微信永久钉在最上面。"""
    for hwnd in list(_TOPMOSTED):
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass
    _TOPMOSTED.clear()


def _pid_of(hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return 0


def _owns_point(hwnd, x, y):
    """这个屏幕坐标点下去，是不是真的落在 hwnd 这个窗口上。

    除了"就是它 / 它的子窗口"，还接受**同一进程的另一个顶层窗口**：
    微信的搜索结果弹层、下拉菜单这些是独立顶层窗口（如 mmui::SearchContentPopover，
    class=Qt51514QWindowToolSaveBits），GA_ROOT 拿到的根跟主窗口不是同一个，
    按老逻辑会被判成"被挡住"而放弃点击 —— 但那层弹窗本来就是要点的目标 UI。
    2026-08-09 实测：搜索"文件传输助手"后点结果项，就栽在这个误判上。

    真正该拦的是"别的程序盖在上面"（面板 Chrome、别的应用），那些进程不同，仍会被拦下。
    """
    try:
        h = win32gui.WindowFromPoint((int(x), int(y)))
        if not h:
            return False
        if _hw(h) == _hw(hwnd) or _hw(win32gui.GetAncestor(h, 2)) == _hw(hwnd):  # GA_ROOT=2
            return True
        pid_hit, pid_target = _pid_of(h), _pid_of(hwnd)
        return bool(pid_hit) and pid_hit == pid_target
    except Exception:
        return True   # 判不了就别拦着


def _invoke(ctrl):
    """不用鼠标坐标的点击兜底：窗口被盖住时坐标点击会打到别的窗口上，
    走 UIA 的 Invoke / DoDefaultAction 直接命中控件本身。"""
    for getter in ("GetInvokePattern", "GetLegacyIAccessiblePattern"):
        try:
            pat = getattr(ctrl, getter)()
            if pat is None:
                continue
            if hasattr(pat, "Invoke"):
                pat.Invoke()
            else:
                pat.DoDefaultAction()
            return True
        except Exception:
            pass
    return False


def _click(ctrl, hwnd, label=""):
    """点之前先确认这一点没被别的窗口盖住，被盖住就重新抬一次，还不行就走 UIA Invoke。"""
    b = ctrl.BoundingRectangle
    cx, cy = (b.left + b.right) // 2, (b.top + b.bottom) // 2
    if not _owns_point(hwnd, cx, cy):
        try:
            log(f"点击点被遮挡（{label} @{cx},{cy}），重新置前；"
                f"挡在这一点上的是 -> {_describe_hwnd(win32gui.WindowFromPoint((int(cx), int(cy))))}")
        except Exception:
            log(f"点击点被遮挡（{label} @{cx},{cy}），重新置前")
        _raise_hwnd(hwnd, f"点击:{label}")
        if not _owns_point(hwnd, cx, cy):
            if _invoke(ctrl):
                log(f"改用 UIA Invoke 点击：{label}")
                return True
            return False
    ctrl.Click(simulateMove=False)
    return True


def _list_editors():
    """当前所有笔记编辑器窗口（Chrome_WidgetWin_0）的 hwnd 集合。"""
    out = set()
    for w in list(auto.GetRootControl().GetChildren()):
        try:
            if w.ClassName == "Chrome_WidgetWin_0":
                out.add(w.NativeWindowHandle)
        except Exception:
            pass
    return out


def _close_all_editors():
    """关掉遗留的笔记编辑器窗口，并确认真的关干净了。返回 (ok, msg)。

    2026-08-01 教训（日报断供 7-31 ~ 8-1 两天）：原来这里只 PostMessage(WM_CLOSE) 就走人，
    从不校验。一旦有窗口关不掉（未保存确认框之类），后面 _top("Chrome_WidgetWin_0","笔记")
    会抓到这个关不掉的旧窗口当成"新建的笔记"，往一个不可编辑的窗口里粘 —— 粘贴校验必然
    读回哨兵串，失败后又留下一个窗口，**下次接着抓到它，自锁**。表现就是连续 5 次
    "内容没粘进笔记编辑器（试了 3 次）"，而环境本身完全正常（同期独立进程里怎么测都通）。
    现在关不干净就直接中止，不在脏状态上硬着头皮往下走。"""
    for hwnd in _list_editors():
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    time.sleep(2.0)
    left = _list_editors()
    if left:
        log(f"still {len(left)} 个编辑器窗口没关掉，再关一次：{sorted(left)}")
        for hwnd in left:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        time.sleep(2.5)
        left = _list_editors()
    if left:
        return False, (f"有 {len(left)} 个笔记编辑器窗口关不掉（多半弹了未保存确认框），"
                       f"再往下走会粘进旧窗口，已中止。去桌面上手动关掉它们")
    return True, ""


def _desktop_usable():
    """桌面锁屏 / 远程会话断开时，整套 UI 自动化是瞎的：没有前台窗口、
    鼠标点击落不到微信、截图直接失败。2026-07-24 就是这么静默失败一整天的
    （报出来的却是"找不到群标题"这种莫名其妙的错）。这里先判掉，给个能看懂的原因。
    解法：在机器上跑 `tscon <会话号> /dest:console` 把会话接回控制台解锁。"""
    for _ in range(3):
        try:
            if win32gui.GetForegroundWindow():
                return True, ""
        except Exception:
            return True, ""
        time.sleep(1.0)
    return False, ("桌面处于锁屏或会话断开状态（取不到前台窗口），点击和键盘全部无效。"
                   "去机器上跑 tscon <会话号> /dest:console 解锁后再发")


def _list_update_windows():
    """当前所有微信"版本更新"弹窗的 hwnd 集合。"""
    out = set()
    for w in list(auto.GetRootControl().GetChildren()):
        try:
            if w.ClassName == "mmui::UpdateWindow":
                out.add(w.NativeWindowHandle)
        except Exception:
            pass
    return out


def _close_update_windows():
    """关掉微信"版本更新"弹窗（mmui::UpdateWindow）。它一旦弹出会挡住发送、
    甚至让 bot 初始化崩溃。发送前先清掉。切记别让微信真升级（wxautox4 只支持到 4.1.9.35）。

    2026-08-03 加校验：原来只 PostMessage(WM_CLOSE) + 固定 sleep 1.5s 就走人，从不复查。
    PostMessage 是异步的，弹窗完全可能没关掉（比如它自己弹了个确认框），而更新弹窗通常
    置顶且持有前台 —— 一个没关掉的更新弹窗正好能解释那天"抢不到前台焦点"。
    现在关完复查，还在就再关一次；两次都关不掉就把这些窗口画像写进日志留证据
    （只记不拦：更新弹窗未必真挡路，为它中止整条发送反而更糟）。"""
    first = _list_update_windows()
    if not first:
        return
    for hwnd in first:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    log(f"关闭了 {len(first)} 个微信更新弹窗：{sorted(first)}")
    time.sleep(1.5)

    left = _list_update_windows()
    if not left:
        return
    log(f"复查：还有 {len(left)} 个更新弹窗没关掉 {sorted(left)}，再关一次")
    for hwnd in left:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    time.sleep(2.0)

    left = _list_update_windows()
    if left:
        log(f"⚠ 更新弹窗关不掉（{len(left)} 个），它多半置顶且占着前台，"
            f"后面抢焦点很可能失败：")
        for hwnd in left:
            log(f"    {_describe_hwnd(hwnd)}")
        _log_fg("此刻")
    else:
        log("复查通过：更新弹窗已全部关掉")


# ---------------- 步骤 ----------------
def _create_note_from_clipboard(cf_bytes, plain, expect):
    """点收藏 -> 新建笔记 -> 粘贴 -> 校验内容真进去了 -> 关闭(自动存入收藏)。

    expect：粘贴后应该能在正文里读到的字串（用当日完整标题）。
    """
    wx = _top("mmui::MainWindow")
    if not wx:
        return False, "主窗口未找到"
    hwnd = wx.NativeWindowHandle
    got_fg = _raise(wx, "主窗口")
    log(f"主窗口置前：前台={got_fg} rect={wx.BoundingRectangle}")
    if not got_fg:
        # 这一行是 2026-08-03 三次失败时唯一的线索，但当时没记"前台被谁占着"，
        # 事后完全查不下去（等去复现，环境已经自己好了）。现在当场留证。
        _log_fg("主窗口没抢到前台，此刻")

    fav = _find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    if not fav:
        return False, "'收藏'入口未找到"
    if not _click(fav, hwnd, "收藏"):
        return False, "'收藏'入口被别的窗口挡住，点不到"
    time.sleep(1.6)

    # "新建笔记"是收藏页左侧栏顶部那个空名图标格（相对窗口实测 L60 T81 R300 B133）。
    # 必须用"相对窗口"坐标：屏幕分辨率会变（远程 1920x1080 / 控制台 1024x768），
    # 窗口位置和大小跟着变，写死屏幕绝对坐标一换分辨率就找不到。
    # 也不能只判 left：收藏停在"笔记"子视图时右侧列表会多出一条空名细表头
    # (相对 L301 T82 高 12px)，会被 DFS 先返回 -> 点到右侧死区、编辑器不开。
    wb = wx.BoundingRectangle
    def _is_new_note(c):
        if c.ClassName != "mmui::XTableCell" or (c.Name or "") != "":
            return False
        b = c.BoundingRectangle
        return (40 < b.left - wb.left < 130            # 左侧栏内（导航条右边）
                and b.right - wb.left < 330            # 不越到右侧列表去
                and 55 <= b.top - wb.top <= 115        # 左侧栏顶部那一格
                and b.bottom - b.top > 30)             # 排除 12px 高的细表头
    cell = _find_in(wx, _is_new_note)
    if not cell:
        return False, "新建笔记入口未找到"
    # 点之前先记下已有的编辑器窗口。点完只认「新冒出来的」那个 —— 只按 class+name 找的话，
    # 会把上一次失败留下的旧窗口当成新笔记（2026-08-01 自锁事故，见 _close_all_editors 注释）。
    before = _list_editors()
    if not _click(cell, hwnd, "新建笔记"):
        return False, "新建笔记入口被别的窗口挡住，点不到"

    note = None
    for _ in range(16):          # 最多等 8 秒，编辑器冷启动有时候慢
        time.sleep(0.5)
        for wd in list(auto.GetRootControl().GetChildren()):
            try:
                if (wd.ClassName == "Chrome_WidgetWin_0" and (wd.Name or "") == "笔记"
                        and wd.NativeWindowHandle not in before):
                    note = wd
                    break
            except Exception:
                pass
        if note:
            break
    if not note:
        if _list_editors() & before:
            return False, "笔记编辑器没新开出来（只剩点击前就存在的旧窗口），已中止"
        return False, "笔记编辑器未打开"
    r = note.BoundingRectangle
    nh = note.NativeWindowHandle
    _raise_hwnd(nh, "笔记编辑器")
    if _hw(win32gui.GetForegroundWindow()) != _hw(nh):
        # 键盘走前台焦点：编辑器不在前台，Ctrl+V 会粘到别的窗口去，
        # 结果存出一条空笔记（或干脆不存），后面还会误发历史笔记。
        _log_fg(f"编辑器(hwnd={nh})第一次没拿到前台，此刻")
        _raise_hwnd(nh, "笔记编辑器-重试")
        if _hw(win32gui.GetForegroundWindow()) != _hw(nh):
            # 中止前把现场记全：谁占着前台、编辑器自己什么状态、有没有更新弹窗挡着。
            # 没有这几行，事后只剩一句"没拿到键盘焦点"，根因无从查起（8/3 就是这样）。
            _log_fg(f"编辑器(hwnd={nh})两次都没拿到前台，中止；此刻")
            log(f"    编辑器窗口自身 -> {_describe_hwnd(nh)}")
            upd = _list_update_windows()
            log(f"    当前微信更新弹窗={len(upd)} {sorted(upd)}")
            for _h in upd:
                log(f"      {_describe_hwnd(_h)}")
            win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
            return False, "笔记编辑器没拿到键盘焦点，怕粘贴落空，已中止"
    # 粘贴 + 硬校验：点完正文，编辑器（Chromium）拿到输入焦点要时间，
    # 早年写死 sleep 0.6s，机器一慢焦点还没到，Ctrl+V 落空 -> 存出空笔记，
    # 后面还会误发历史笔记。所以粘完必须 Ctrl+A/Ctrl+C 读回来比对，不通过就重来。
    #
    # 落点：按正文 DocumentControl 的实时 rect 取中心，别再用「窗口顶部 + 固定像素」。
    # 2026-08-01 实测（编辑器 701x641）：正文顶部往下 60px 处点不进去（那是标题行，
    # 不可编辑，Ctrl+V 落空），89px 开始才行 —— 而老代码的 r.top+130 正好落在 89px，
    # 离失效边界只剩 29px。窗口被拖动或微信记住新尺寸就会翻车，跟屏幕分辨率是否固定无关。
    doc = _find_in(note, lambda c: c.ControlTypeName == "DocumentControl")
    if doc:
        db = doc.BoundingRectangle
        cx, cy = (db.left + db.right) // 2, (db.top + db.bottom) // 2
        log(f"笔记编辑器 hwnd={nh} rect={r} 正文={db} 落点=({cx},{cy})")
    else:
        cx, cy = (r.left + r.right) // 2, r.top + 130
        log(f"笔记编辑器 hwnd={nh} rect={r} 找不到正文控件，回落老坐标 ({cx},{cy})")
    ok_paste = False
    for attempt in range(1, 4):
        _clip_html(cf_bytes, plain)
        auto.Click(cx, cy)
        time.sleep(1.8)
        _fg = win32gui.GetForegroundWindow()
        _hit = win32gui.WindowFromPoint((cx, cy))
        log(f"  粘贴前：前台={_fg}({win32gui.GetClassName(_fg) if _fg else ''}) "
            f"期望={nh} 点归属编辑器={_owns_point(nh, cx, cy)}({win32gui.GetClassName(_hit) if _hit else ''})")
        auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
        auto.SendKeys("{Ctrl}v", waitTime=0.1); time.sleep(2.5)
        _clip_text("___CHK___"); time.sleep(0.4)
        auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
        auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
        back = _clip_read()
        if back and expect in str(back):
            ok_paste = True
            log(f"粘贴校验通过（第 {attempt} 次）")
            break
        log(f"粘贴校验未通过（第 {attempt} 次），读回={str(back)[:40]!r}，重试")
    if not ok_paste:
        win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
        time.sleep(2.0)
        return False, "内容没粘进笔记编辑器（试了 3 次），已中止"
    win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
    time.sleep(2.0)
    return True, "ok"


def _find_note_cell(title_kw):
    """在收藏列表里找标题含 title_kw 的笔记格（主窗口收藏页里）。"""
    wx = _top("mmui::MainWindow")
    if not wx:
        return None
    return _find_in(wx, lambda c: (c.ClassName == "mmui::XTableCell"
                                   and title_kw in (c.Name or "")))


def _open_target(target):
    """定位目标聊天，返回 (ok, win, msg)。win 是后面点"发送收藏"的容器窗口。

    优先用独立聊天窗口：bot 的监听器会把被监听的群弹成 mmui::ChatSingleWindow，
    窗口标题就是群名本身 —— 比"主窗口搜索 + 猜标题栏"可靠得多，也不会发错群。
    没有独立窗口才退回搜索。
    """
    cw = _top("mmui::ChatSingleWindow", target)
    if cw:
        if not _raise(cw, f"独立聊天窗口:{target}"):
            _log_fg("独立聊天窗口没抢到前台，此刻")
        return True, cw, f"独立聊天窗口「{target}」"

    wx = _top("mmui::MainWindow")
    if not wx:
        return False, None, "主窗口未找到"
    hwnd = wx.NativeWindowHandle
    if not _raise(wx, "主窗口(搜索目标)"):
        _log_fg("主窗口没抢到前台，此刻")

    chat_tab = _find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "微信")
    if chat_tab:
        _click(chat_tab, hwnd, "微信tab")
    time.sleep(1.0)
    wb = wx.BoundingRectangle   # 相对窗口坐标，换分辨率不失灵
    sb = _find_in(wx, lambda c: (c.ControlTypeName == "EditControl" and (c.Name or "") == "搜索"
                                 and c.BoundingRectangle.top - wb.top < 120
                                 and c.BoundingRectangle.left - wb.left < 400))
    if not sb:
        return False, None, "搜索框未找到"
    if not _click(sb, hwnd, "搜索框"):
        return False, None, "搜索框被别的窗口挡住，点不到"
    time.sleep(0.6)
    if _hw(win32gui.GetForegroundWindow()) != _hw(hwnd):
        # 键盘走的是前台焦点，没拿到焦点粘贴会进别的窗口 —— 直接判失败，别静默跑空。
        _force_foreground(hwnd)
        time.sleep(0.5)
        if _hw(win32gui.GetForegroundWindow()) != _hw(hwnd):
            _log_fg(f"搜索框输入前主窗口(hwnd={hwnd})没拿到前台，中止；此刻")
            return False, None, "微信没拿到键盘焦点（被别的窗口占着），中止"
    _clip_text(target)
    auto.SendKeys("{Ctrl}a", waitTime=0.1)
    auto.SendKeys("{Ctrl}v", waitTime=0.1)

    core = target.strip()[-6:]   # 取目标名尾部若干字，避开 emoji 前缀
    # 轮询等搜索结果出现（固定 sleep 撞上微信卡顿就废），出现后直接点结果项。
    # 不用盲按回车：回车选中的是当前高亮项，排序一变就打开错的聊天。
    cell = None
    for _ in range(24):
        time.sleep(0.5)
        cell = _find_in(wx, lambda c: (core in (c.Name or "")
                                       and c.BoundingRectangle.left - wb.left < 420
                                       and c.BoundingRectangle.right - c.BoundingRectangle.left > 100
                                       and c.BoundingRectangle.bottom - c.BoundingRectangle.top > 20))
        if cell:
            break
    if not cell:
        return False, None, f"搜索结果里没出现 '{core}'（搜索没生效或群名对不上），中止发送"
    if not _click(cell, hwnd, "搜索结果"):
        return False, None, "搜索结果项被挡住，点不到"

    wt = wx.BoundingRectangle.top   # 用窗口相对坐标，窗口挪位置也不会失灵
    hdr = None
    for _ in range(20):
        time.sleep(0.5)
        hdr = _find_in(wx, lambda c: (c.ControlTypeName == "TextControl"
                                      and core in (c.Name or "")
                                      and c.BoundingRectangle.top - wt < 160))
        if hdr:
            break
    if not hdr:
        return False, None, f"未确认打开目标（找不到标题含 '{core}'），中止发送"
    return True, wx, f"主窗口聊天「{hdr.Name}」"


def _send_favorite(win, title_kw):
    """在 win 这个聊天窗口点'发送收藏' -> 选中标题含 title_kw 的笔记 -> 点'发送'。"""
    hwnd = win.NativeWindowHandle
    got_fg = _raise(win, "聊天窗口(发送收藏)")
    log(f"聊天窗口置前：前台={got_fg}")
    if not got_fg:
        _log_fg("聊天窗口没抢到前台，此刻")
    btn = _find_in(win, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "发送收藏")
    if not btn:
        return False, "'发送收藏'按钮未找到"
    if not _click(btn, hwnd, "发送收藏"):
        return False, "'发送收藏'按钮被挡住，点不到"
    time.sleep(2.2)
    # 选择器可能挂在本窗口里，也可能是独立顶层窗口，两边都找。
    pred_cell = lambda c: c.ClassName == "mmui::XTableCell" and (title_kw in (c.Name or ""))
    cell = _find_in(win, pred_cell) or _find_global(pred_cell)
    if not cell:
        return False, "选择器里未找到目标笔记"
    cell.Click(simulateMove=False)
    time.sleep(1.0)
    # 选择器"发送"按钮用宽度区分（约106px），聊天输入框的"发送"约56px。
    # 不能用绝对坐标范围——微信窗口高度会变(实测 815/908)导致按钮 top 漂移、定位失败。
    pred_send = lambda c: (c.ControlTypeName == "ButtonControl" and c.Name == "发送"
                           and (c.BoundingRectangle.right - c.BoundingRectangle.left) > 80)
    sendb = _find_in(win, pred_send) or _find_global(pred_send)
    if not sendb:
        return False, "选择器'发送'按钮未找到"
    sendb.Click(simulateMove=False)
    time.sleep(3.0)
    return True, "sent"


# ---------------- 新鲜度 / 防重 ----------------
def _fresh_enough(d):
    ga = d.get("generated_at")
    if not ga:
        return True
    try:
        t = datetime.datetime.strptime(ga, "%Y-%m-%d %H:%M:%S")
        age = (datetime.datetime.now() - t).total_seconds() / 3600.0
        return age <= config.MAX_AGE_HOURS
    except Exception:
        return True


def _already_sent_today():
    try:
        with open(_STATE, encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except Exception:
        return False


def _mark_sent_today():
    try:
        with open(_STATE, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
    except Exception:
        pass


# ---------------- webhook 通知 ----------------
def _push(msg, source):
    """把结果推到主程序 webhook（飞书群）。失败不影响主流程。"""
    try:
        from webhook_send import send_message
        tag = "手动" if source == "manual" else "定时"
        send_message(f"🐶 AI 日报 · {tag}", msg)
    except Exception as e:
        log(f"webhook 推送失败: {e!r}")


# ---------------- 主入口 ----------------
def send_daily_note(bot=None, force=False, source="scheduled"):
    """执行一次完整发送。返回结果字符串。schedule 每日调用，或面板手动触发（source='manual'）。

    关键结果（成功/失败/异常/数据异常）webhook 推送到飞书群；正常防重跳过不推（避免噪音）。
    """
    def done(msg, push=True):
        log(msg)
        if push:
            _push(msg, source)
        return msg

    try:
        if not config.ENABLED:
            return done("已禁用（config.ENABLED=False），跳过", push=False)
        if not force and _already_sent_today():
            return done("今天已发过，跳过（防重）", push=False)
        if not os.path.exists(config.DATA_FILE):
            return done(f"⚠️ 数据文件不存在：{config.DATA_FILE}，跳过")
        with open(config.DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        # 今天日报就位校验（手动/自动都强制过，force 不跳过）：
        # latest.json 的 date 必须是今天，否则说明今天的日报还没推来（或推送失败），
        # 宁可不发也别把昨天的旧日报当今天的发出去。
        today = datetime.date.today().isoformat()
        data_date = d.get("date")
        if data_date != today:
            return done(f"⚠️ 今天（{today}）的日报还没就位，当前数据日期：{data_date or '无'}，未发送。"
                        f"（等 mac-mini 09:00 的日报推来、或检查推送链路后再发）")
        if not force and not _fresh_enough(d):
            return done(f"⚠️ 数据过旧(>{config.MAX_AGE_HOURS}h)，跳过。generated_at={d.get('generated_at')}")
        if not d.get("items"):
            return done("⚠️ 日报无 items，跳过")

        title, frag, hits = render(d)   # hits = 审查命中并打码的敏感词
        cf_bytes = _build_cf_html(frag)
        _clip_html(cf_bytes, title)
        title_kw = title.replace("🐶", "").strip()   # 当日完整标题（含日期）
        if hits:
            log(f"内容审查：已打码敏感词 {hits}")
        log(f"开始发送日报笔记：{title} -> {config.TARGET}（{d.get('count')} 条）")

        ok, why = _desktop_usable()
        if not ok:
            return done(f"❌ 发送失败（环境）：{why}")

        _close_update_windows()   # 发送前清掉微信更新弹窗，防止挡住流程
        ok, why = _close_all_editors()
        if not ok:
            return done(f"❌ 发送失败（环境）：{why}")
        ok, msg = _create_note_from_clipboard(cf_bytes, title, title_kw)
        if not ok:
            return done(f"❌ 发送失败（建笔记）：{msg}")

        # 再从收藏列表确认这条笔记真的入库了。后面选笔记也一律用"当日完整标题"，
        # 不能只用 TITLE_PREFIX：收藏里堆着历史日报，前缀匹配会命中昨天那条 ——
        # 实测就这么把 7月23 日的旧日报当今天的发出去过。宁可找不到中止，也不能发错。
        cell = None
        for _ in range(10):
            time.sleep(1.0)
            cell = _find_note_cell(title_kw)
            if cell:
                break
        if not cell:
            return done(f"❌ 发送失败（建笔记）：收藏里没出现「{title_kw}」，"
                        f"多半是粘贴没落进编辑器，中止发送（不会拿旧日报顶数）")
        log(f"已确认新笔记入库：{title_kw}")

        ok, win, msg = _open_target(config.TARGET)
        if not ok:
            return done(f"❌ 发送失败（打开目标）：{msg}")
        log(f"已打开目标：{msg}")

        ok, msg = _send_favorite(win, title_kw)
        if not ok:
            return done(f"❌ 发送失败（发送收藏）：{msg}")

        _mark_sent_today()
        extra = f"，已过滤敏感词：{'、'.join(hits)}" if hits else ""
        return done(f"✅ 日报已发送到 {config.TARGET}（{d.get('count')} 条）{extra}")
    except Exception as e:
        import traceback
        log(traceback.format_exc())
        return done(f"❌ 发送异常：{e}")
    finally:
        _drop_topmost()   # 别把微信永久钉在最上层，挡住人操作


# ---------------- 降级通路：不新建任何窗口，直接发纯文本 ----------------
# 2026-08-12 事故：微信进入"已有窗口读写正常、但新顶层窗口一个都建不出来"的状态
# （同期探针 AddListenChat 也成块失败，报 error(1400,'MoveWindow')），建笔记连败
# 4 次、日报整天断供；而**往已存在的窗口发文本全程正常**（bot 收发消息、告警都照发）。
# 所以降级 = 不点"新建笔记"、不开任何顶层窗口，把日报当纯文本发进目标群已有的会话。
# 降级只是止血：正常笔记流程一行不改，guarded 入口先原样跑完 send_daily_note，
# 只在它已失败、且**能确定群里还什么都没收到**时才追加发文本。
_DEGRADE_STATE = os.path.join(os.path.dirname(__file__), "data", "degrade_state.json")

# 允许降级的失败阶段（取自返回文案的"（阶段）"）。入选判据只有一条：这四个阶段全部发生
# 在"点选择器里的【发送】按钮"之前，群里一定还什么都没收到，降级不可能重复推送。逐个核
# 对过：环境=_desktop_usable/_close_all_editors 挂；建笔记=_create_note_from_clipboard 挂
# 或收藏里没这条；打开目标=_open_target 挂；发送收藏=_send_favorite 的每个 False 返回都在
# sendb.Click() 之前（点完发送就直接 return True）。故意**不含"发送异常"**：那是 try 里任
# 意一行抛的，确定不了发送点过没点过 —— 宁可不发，也不冒"笔记已进群又补一份文本"的风险。
_DEGRADE_SAFE_STAGES = ("环境", "建笔记", "打开目标", "发送收藏")


def _fail_stage(text):
    """从返回文案取出失败阶段；不是"发送失败"就返回 None。
    ✅/⚠️/❌ 三态是 trigger._status_of 早就依赖的既有约定，这里只多解析一层括号里的阶段名。"""
    t = str(text)
    head = "❌ 发送失败（"
    if not t.startswith(head):
        return None
    return t[len(head):].split("）", 1)[0]


def _degrade_state():
    """读今天的降级状态。日期不是今天就当空的返回（跨天自动归零，不用清理）。"""
    today = datetime.date.today().isoformat()
    try:
        with open(_DEGRADE_STATE, encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict) and s.get("date") == today:
            return {"date": today,
                    "fail_count": int(s.get("fail_count") or 0),
                    "first_fail_at": s.get("first_fail_at"),
                    "degraded_at": s.get("degraded_at")}
    except Exception:
        pass
    return {"date": today, "fail_count": 0, "first_fail_at": None, "degraded_at": None}


def _save_degrade_state(s):
    """原子写降级状态。**必须落盘**：计数放内存里，探针自愈重启一次就全丢
    （2026-08-12 有 5 次自愈重启，09:45 那次直接把重试链冲掉），降级就永远等不到门槛。"""
    try:
        os.makedirs(os.path.dirname(_DEGRADE_STATE), exist_ok=True)
        tmp = _DEGRADE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DEGRADE_STATE)
    except Exception as e:
        log(f"写 degrade_state.json 失败（不影响本次降级判定）：{e!r}")


def _should_degrade(text):
    """决定要不要降级，返回 (要不要, 不降级的原因)。
    顺带把"今天第几次安全失败/首次失败在什么时候"记进状态文件 —— 触发门槛就靠它。"""
    stage = _fail_stage(text)
    if stage is None:
        return False, ""                      # 成功 / ⚠️跳过 / 发送异常：都不降级，也不记账
    if not getattr(config, "DEGRADE_ENABLED", False):
        return False, "降级通路已关闭（config.DEGRADE_ENABLED=False）"
    if stage not in _DEGRADE_SAFE_STAGES:
        return False, f"失败阶段「{stage}」不在安全清单里，无法确定笔记有没有已经发进群，不降级"
    if _already_sent_today():
        return False, "last_sent.txt 已是今天（笔记通道可能已经发出去了），不降级"

    s = _degrade_state()
    if s.get("degraded_at"):
        return False, f"今天已经降级发过了（{s['degraded_at']}），不重复发"
    s["fail_count"] += 1
    if not s.get("first_fail_at"):
        s["first_fail_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    _save_degrade_state(s)

    waited = 0.0
    try:
        t0 = datetime.datetime.fromisoformat(s["first_fail_at"])
        waited = (datetime.datetime.now() - t0).total_seconds() / 60.0
    except Exception:
        pass
    need_fails = int(getattr(config, "DEGRADE_AFTER_FAILS", 3))
    need_min = float(getattr(config, "DEGRADE_AFTER_MIN", 40))
    if s["fail_count"] >= need_fails:
        return True, ""
    if waited >= need_min:
        return True, ""
    return False, (f"今天第 {s['fail_count']} 次失败、距首次失败 {waited:.0f} 分钟，"
                   f"还没到降级门槛（≥{need_fails} 次 或 ≥{need_min:.0f} 分钟），先让笔记流程再试")


def _hard_split(block, limit, bot=None):
    """单个条目块本身就超过一条消息的字数上限时，按字数硬切（会切断句子，最后手段）。
    切分复用 bot 那套 split_long_text，别自己再写一份切法。"""
    try:
        return list(bot.config.split_long_text(block, limit))
    except Exception:
        return [block[i:i + limit] for i in range(0, len(block), limit)]


def _pack_blocks(blocks, limit, bot=None):
    """把条目块装进若干条消息，**一个条目块绝不跨消息切开**。
    不直接对整篇正文调 split_long_text(2000)：按字数盲切的切点会落在 URL 中间，断链微信
    不识别成蓝链，等于这条日报废了。所以按"条目"这个天然边界打包。"""
    segs, cur, n = [], [], 0
    for b in blocks:
        if len(b) > limit:
            if cur:
                segs.append("\n\n".join(cur))
                cur, n = [], 0
            segs.extend(_hard_split(b, limit, bot))
            continue
        add = len(b) + (2 if cur else 0)      # 块之间用一个空行分隔
        if cur and n + add > limit:
            segs.append("\n\n".join(cur))
            cur, n = [], 0
            add = len(b)
        cur.append(b)
        n += add
    if cur:
        segs.append("\n\n".join(cur))
    return segs


def _degrade_messages(d, bot=None):
    """把日报渲染成"可以直接一条条发出去"的纯文本消息列表。返回 (messages, title, hits)。"""
    mode = str(getattr(config, "DEGRADE_MODE", "digest") or "digest").strip()
    title, blocks, hits = render_plain(d, mode)
    limit = max(300, int(getattr(config, "DEGRADE_SEG_CHARS", 1500)))
    segs = _pack_blocks(blocks, limit, bot)

    # 上限内发不完就截断：宁可少发几条也别刷屏（连发十几条长文本容易被判骚扰），
    # 末尾注明少了多少，人可以去服务器上看完整版。
    cap = max(1, int(getattr(config, "DEGRADE_MAX_SEGMENTS", 6)))
    dropped = max(0, len(segs) - cap)
    segs = segs[:cap]

    note = str(getattr(config, "DEGRADE_NOTE", "（笔记通道故障，本次改为文本推送）"))
    total = len(segs)
    messages = []
    for i, seg in enumerate(segs, 1):
        head = title if total == 1 else f"{title}（{i}/{total}）"
        if i == 1:
            head = f"{head}\n{config.INTRO}\n{note}"
        tail = ""
        if i == total and dropped:
            tail = f"\n\n（内容过长，还有 {dropped} 段未发送，完整版见服务器 {config.DATA_FILE}）"
        messages.append(f"{head}\n\n{seg}{tail}")
    return messages, title, hits


def _send_ok(r):
    """判 wxautox 返回是否发送成功。照 wxbot_core 的 ReplyCountStore.was_send_success 抄
    一份，故意不 import 它（插件被 wxbot_core 导入，反向 import 会循环依赖）。"""
    if r is True:
        return True
    if r is False or r is None:
        return False
    if isinstance(r, dict):
        st = str(r.get("status", "")).lower()
        if st in ("success", "ok", "true", "成功"):
            return True
        if st in ("error", "fail", "failed", "false", "失败", "错误"):
            return False
    return bool(r)


def _send_text_to_target(wx, target, messages, dry_run=False):
    """把 messages 逐条发到 target。返回 (发成功几条, 用的哪条通道)。

    **全程不新建任何窗口**，这是降级路径的立命之本：
      首选 wx.GetSubWindow(target)：目标群一直被 bot 监听，独立窗口本来就开着，不碰
            "新建窗口"那条坏路（故障期实测对 5 个已有窗口五连成功、窗口数不变）。
      回落 wx.SendMsg(who=..., exact=True)：主窗口切会话再发，也不新建顶层窗口，就是
            MainWindowChat 那条通道。exact=True 防发错群（群名带 emoji 后缀）。
    绝不做 SetForegroundWindow / 置顶 / 点击：抢前台是给建笔记用的，外层 finally 已经
    _drop_topmost 过了，发文本让 wxautox 自己处理。
    """
    chat, how = None, ""
    try:
        sub = wx.GetSubWindow(target)
    except Exception as e:
        sub = None
        log(f"GetSubWindow 出错，回落主窗口发送：{e!r}")
    # 校验 who 完全相等：宁可回落到 exact 搜索，也不能往一个名字不对的窗口里发日报。
    if sub is not None and str(getattr(sub, "who", "") or "") == target:
        chat, how = sub, f"已有独立窗口「{target}」"
    else:
        how = f"主窗口切会话（exact 匹配「{target}」）"
    log(f"降级发送：通道={how}，共 {len(messages)} 条"
        f"{'（演练模式，一条都不真发）' if dry_run else ''}")

    interval = max(0.0, float(getattr(config, "DEGRADE_SEG_INTERVAL_SEC", 4.0)))
    sent = 0
    for i, m in enumerate(messages, 1):
        if dry_run:
            log(f"  [演练] 第 {i}/{len(messages)} 条（{len(m)} 字）：\n{m}")
            sent += 1
            continue
        try:
            r = chat.SendMsg(m) if chat is not None else wx.SendMsg(msg=m, who=target, exact=True)
        except Exception as e:
            log(f"  第 {i}/{len(messages)} 条发送抛异常，停止后续发送：{e!r}")
            break
        if not _send_ok(r):
            log(f"  第 {i}/{len(messages)} 条发送失败，停止后续发送：{r!r}")
            break
        sent += 1
        log(f"  第 {i}/{len(messages)} 条已发（{len(m)} 字）")
        if i < len(messages):
            time.sleep(interval)   # 节流：连发不歇容易触发风控，也刷屏
    return sent, how


def _degrade_send(bot, orig_text, source):
    """执行一次降级发送。返回给 trigger 用的结果文案（首字符仍遵守 ✅/⚠️/❌ 约定）。"""
    target = str(config.TARGET or "").strip()
    if not target:
        msg = f"{orig_text}｜降级未执行：目标群名为空（settings.json 的 target）"
        log(msg)
        return msg

    wx = getattr(bot, "wx", None)
    if wx is None:
        # 面板"手动发送"（web_server.py:1248）直接调 send_daily_note(bot=None)，拿不到客户端
        # 对象；那条路本来也不该在 web 线程里碰微信 UI，这里只如实说明，不硬凑一个 WeChat。
        msg = f"{orig_text}｜降级未执行：拿不到 bot 的微信客户端（bot.wx 未初始化）"
        log(msg)
        return msg

    # 重新读数据并复查日期：降级距笔记流程已隔几十分钟，期间 mac-mini 可能又覆盖了
    # latest.json。绝不能把昨天的旧日报当今天的发出去。
    try:
        with open(config.DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        msg = f"{orig_text}｜降级未执行：读不了 {config.DATA_FILE}（{e!r}）"
        log(msg)
        return msg
    today = datetime.date.today().isoformat()
    if d.get("date") != today or not d.get("items"):
        msg = (f"{orig_text}｜降级未执行：数据日期={d.get('date')}（今天 {today}）"
               f"或 items 为空，不发")
        log(msg)
        return msg

    messages, title, hits = _degrade_messages(d, bot)
    if hits:
        log(f"降级内容审查：已打码敏感词 {hits}")
    log(f"启动降级发送：{title} -> {target}，mode={getattr(config, 'DEGRADE_MODE', 'digest')} "
        f"共 {len(messages)} 条 / {sum(len(m) for m in messages)} 字；"
        f"笔记通道失败原因：{orig_text}")

    dry_run = bool(getattr(config, "DEGRADE_DRY_RUN", False))
    sent, how = _send_text_to_target(wx, target, messages, dry_run)

    if dry_run:
        # 演练不动 last_sent.txt / degrade_state.json：可以反复跑，也不挡正常笔记流程重试。
        out = f"⚠️ 降级演练完成（{sent}/{len(messages)} 条只写日志未发送），原因：{orig_text}"
        log(out)
        return out

    extra = f"，已过滤敏感词：{'、'.join(hits)}" if hits else ""
    if sent >= len(messages) and sent > 0:
        # 写 last_sent.txt 是**防重复推送的关键一步**：后续所有重试与今天的定时任务都会在
        # send_daily_note 开头被"今天已发过"拦掉，笔记不可能再补发一遍。副作用（是期望
        # 行为）：gh_trending_note 靠这个文件判断"AI 日报已发完"，会随之放行。
        _mark_sent_today()
        s = _degrade_state()
        s["degraded_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save_degrade_state(s)
        out = (f"✅ 日报已降级为纯文本发送到 {target}（{len(messages)} 条，"
               f"通道：{how}）{extra}。笔记通道失败原因：{orig_text}")
        log(out)
        _push(out, source)
        return out

    if sent > 0:
        # 发了一半：群里已有内容，绝不能让笔记流程再补一份 —— 照样写 last_sent.txt
        # 掐掉重试，然后大声喊人来收尾。
        _mark_sent_today()
        s = _degrade_state()
        s["degraded_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save_degrade_state(s)
        out = (f"⚠️ 降级只发出 {sent}/{len(messages)} 条到 {target}（通道：{how}），"
               f"群里已有内容、已按防重记为今天已发，请人工确认并补齐剩余部分。"
               f"笔记通道失败原因：{orig_text}")
        log(out)
        _push(out, source)
        return out

    out = f"{orig_text}｜降级也没发出去（0/{len(messages)} 条，通道：{how}），等下次重试"
    log(out)
    return out


def send_daily_note_guarded(bot=None, force=False, source="scheduled"):
    """正常笔记流程 + 失败后的纯文本降级。**trigger 调这个，不要直接调 send_daily_note。**

    send_daily_note 一行没改：先原样跑完它，只在它已失败、能确定"群里还什么都没收到"
    （见 _DEGRADE_SAFE_STAGES）、且失败次数/时间到门槛时，才追加一次不新建窗口的文本
    发送。降级自身出任何岔子都只记日志、原结果照原样返回，不能反过来把主流程搞挂。
    """
    text = send_daily_note(bot, force=force, source=source)
    try:
        ok, why = _should_degrade(text)
        if not ok:
            if why:
                log(f"不降级：{why}")
            return text
        return _degrade_send(bot, text, source)
    except Exception as e:
        import traceback
        log(f"降级通路自身出错（原结果照原样返回）：{e!r}\n{traceback.format_exc()}")
        return text
