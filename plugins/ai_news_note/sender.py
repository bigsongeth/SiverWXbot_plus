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
from .render import render

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
