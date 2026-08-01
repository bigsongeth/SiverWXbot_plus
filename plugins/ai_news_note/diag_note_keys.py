# -*- coding: utf-8 -*-
"""诊断第二轮：笔记编辑器「鼠标有效、键盘无效」到底卡在哪一环。

第一轮（diag_note_editor.py）已排除落点问题：生产落点和正文 DocumentControl
正中心两个位置都粘不进去，而同一次运行里鼠标点击「收藏」「新建笔记」全都生效。
所以问题在键盘 SendInput 这一侧。

本轮把「粘贴」拆成三段独立验证，不再依赖 Ctrl+C 回读（回读本身就可能是坏的那一环）：
  1. 普通字符打字   -> 键盘通道到底通不通
  2. Ctrl+V 粘贴    -> 组合键通不通（用 UIA TextPattern 读正文，不用剪贴板验证）
  3. Ctrl+A/Ctrl+C  -> 生产用的回读校验通不通
外加：全局修饰键状态（远端卡住一个 Shift/Alt 会让所有组合键变成废键，
是「鼠标好使键盘不好使」的头号嫌疑），以及卡住时强制释放后重试。

用法（会话 2）：python plugins/ai_news_note/diag_note_keys.py
副作用：同第一轮 —— 抢前台、开一个笔记编辑器、结束时关掉，可能在收藏留一条测试笔记。
输出 panel_logs/diag_note_keys.log。
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "diag_note_keys.log")

TYPE_PROBE = "DIAGTYPE123"
PASTE_PROBE = "DIAGPASTE456"

_MODS = [
    ("VK_SHIFT", 0x10), ("VK_CONTROL", 0x11), ("VK_MENU(Alt)", 0x12),
    ("VK_LWIN", 0x5B), ("VK_RWIN", 0x5C),
    ("VK_LSHIFT", 0xA0), ("VK_RSHIFT", 0xA1),
    ("VK_LCONTROL", 0xA2), ("VK_RCONTROL", 0xA3),
    ("VK_LMENU", 0xA4), ("VK_RMENU", 0xA5),
    ("VK_CAPITAL(CapsLock)", 0x14), ("VK_NUMLOCK", 0x90),
]


def w(line=""):
    s = str(line)
    try:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass
    try:
        print(s)
    except Exception:
        pass


def _dump_mods(tag):
    import ctypes
    u = ctypes.windll.user32
    stuck = []
    parts = []
    for name, vk in _MODS:
        a = u.GetAsyncKeyState(vk)
        s = u.GetKeyState(vk)
        down = bool(a & 0x8000) or bool(s & 0x8000)
        toggled = bool(s & 0x0001)
        if down:
            stuck.append((name, vk))
        parts.append(f"{name}={'DOWN' if down else 'up'}{'/toggled' if toggled else ''}")
    w(f"[{tag}] 修饰键: " + "  ".join(parts))
    if stuck:
        w(f"[{tag}] !! 卡住的键: {[n for n, _ in stuck]} —— 组合键会被它污染成废键")
    return stuck


def _release_mods(stuck):
    """强制抬起卡住的修饰键。"""
    import ctypes
    u = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    for name, vk in stuck:
        try:
            u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
            w(f"  已发送 keyup: {name}")
        except Exception as e:
            w(f"  keyup 失败 {name}: {e!r}")
    time.sleep(0.4)


def _doc_text(doc):
    """用 UIA TextPattern 读正文。

    ⚠️ 2026-08-01 实测：这个读法在微信笔记编辑器上**不可靠** —— 内容明明粘进去了，
    它读回来的还是占位符 '￼\n￼\n￼\n￼'。所以本脚本里凡是基于它的 ✅/❌ 只当参考，
    真正的判定一律以 Ctrl+A/Ctrl+C 回读剪贴板为准（那个是准的）。
    保留它只是为了对照，别再拿它下结论。"""
    for getter in ("GetTextPattern", "GetValuePattern"):
        try:
            pat = getattr(doc, getter)()
            if pat is None:
                continue
            if getter == "GetTextPattern":
                return pat.DocumentRange.GetText(-1)
            return pat.Value
        except Exception:
            pass
    try:
        return doc.Name or ""
    except Exception:
        return ""


def main():
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# diag_note_keys @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from wxautox4.uia import uiautomation as auto
    import win32gui
    import win32con
    from plugins.ai_news_note import sender as S

    ok, why = S._desktop_usable()
    w(f"桌面可用: {ok} {why}")
    if not ok:
        return

    # ---- 0. 进程完整性级别（UIPI：低完整性进程发不了输入给高完整性窗口）----
    try:
        import ctypes
        import win32process
        import win32api
        hwnd_wx = None
        for c in auto.GetRootControl().GetChildren():
            if c.ClassName == "mmui::MainWindow":
                hwnd_wx = c.NativeWindowHandle
                break
        if hwnd_wx:
            pid = win32process.GetWindowThreadProcessId(hwnd_wx)[1]
            w(f"微信 pid={pid} / 本进程 pid={os.getpid()}")
        w(f"本进程是否管理员: {bool(ctypes.windll.shell32.IsUserAnAdmin())}")
    except Exception as e:
        w(f"进程信息取不到: {e!r}")

    stuck0 = _dump_mods("开场")

    S._close_update_windows()
    S._close_all_editors()

    wx = S._top("mmui::MainWindow")
    if not wx:
        w("!! 主窗口未找到")
        return
    hwnd = wx.NativeWindowHandle
    w(f"主窗口 rect={wx.BoundingRectangle} 置前={S._raise(wx)}")
    wb = wx.BoundingRectangle

    fav = S._find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    if not fav:
        w("!! '收藏' 未找到")
        return
    S._click(fav, hwnd, "收藏")
    time.sleep(1.6)

    def _is_new_note(c):
        if c.ClassName != "mmui::XTableCell" or (c.Name or "") != "":
            return False
        b = c.BoundingRectangle
        return (40 < b.left - wb.left < 130 and b.right - wb.left < 330
                and 55 <= b.top - wb.top <= 115 and b.bottom - b.top > 30)

    cell = S._find_in(wx, _is_new_note)
    if not cell:
        w("!! 新建笔记入口未找到")
        return
    S._click(cell, hwnd, "新建笔记")
    time.sleep(3.0)

    note = S._top("Chrome_WidgetWin_0", "笔记")
    if not note:
        w("!! 笔记编辑器未打开")
        return
    r = note.BoundingRectangle
    nh = note.NativeWindowHandle
    w(f"编辑器 hwnd={nh} rect={r} 置前={S._raise_hwnd(nh)} 前台={win32gui.GetForegroundWindow()}")

    doc = S._find_in(note, lambda c: c.ControlTypeName == "DocumentControl")
    if not doc:
        w("!! 正文 DocumentControl 未找到，只能靠剪贴板校验")
    b = doc.BoundingRectangle if doc else r
    cx, cy = (b.left + b.right) // 2, (b.top + b.bottom) // 2
    auto.Click(cx, cy)
    time.sleep(1.5)
    w(f"已点正文 ({cx},{cy})")
    base = _doc_text(doc) if doc else ""
    w(f"初始正文（TextPattern）={base[:60]!r}")

    _dump_mods("点完正文")

    def _try_type(tag):
        auto.SendKeys(TYPE_PROBE, waitTime=0.05)
        time.sleep(1.5)
        t = _doc_text(doc) if doc else ""
        w(f"[{tag}] 1) 普通字符打字 -> TextPattern 读回={t[:40]!r}（不可靠，仅参考）")

    def _try_paste(tag):
        S._clip_text(PASTE_PROBE)
        time.sleep(0.4)
        auto.SendKeys("{Ctrl}v", waitTime=0.1)
        time.sleep(2.0)
        t = _doc_text(doc) if doc else ""
        w(f"[{tag}] 2) Ctrl+V 粘贴 -> TextPattern 读回={t[:40]!r}（不可靠，仅参考）")

    def _try_copyback(tag):
        """唯一可信的判定：把编辑器内容 Ctrl+A/Ctrl+C 抓回剪贴板再看。

        先把剪贴板压成哨兵串，若回读后剪贴板变了、且含前面打/粘进去的探针，
        就证明键盘和粘贴这两条通道都是好的。"""
        S._clip_text("___CHK___")
        time.sleep(0.4)
        auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
        auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
        back = str(S._clip_read() or "")
        typed = TYPE_PROBE in back
        pasted = PASTE_PROBE in back
        w(f"[{tag}] 3) Ctrl+A/Ctrl+C 回读 -> {back[:70]!r}")
        w(f"[{tag}]    打字进去了={typed}  粘贴进去了={pasted}  "
          f"（回读本身{'有效' if back and '___CHK___' not in back else '无效——剪贴板还是哨兵'}）")
        return typed, pasted

    w("== 第一轮（原样）==")
    _try_type("原样")
    _try_paste("原样")
    t1, p1 = _try_copyback("原样")

    t2 = p2 = None
    if not (t1 and p1):
        w("== 第二轮（强制释放全部修饰键后重试）==")
        _release_mods(_MODS)          # 不管当前读数如何，全部抬一遍
        _dump_mods("释放后")
        auto.Click(cx, cy)
        time.sleep(1.2)
        _try_type("释放后")
        _try_paste("释放后")
        t2, p2 = _try_copyback("释放后")

    w("== 判定 ==")
    tt = t2 if t2 is not None else t1
    pp = p2 if p2 is not None else p1
    if tt and pp:
        w("打字和粘贴都进去了 -> 键盘通道完全正常，别再往 SendInput/钩子方向查")
        w("   生产还失败的话，去查「粘的是哪个窗口」—— 抓到上次失败留下的旧编辑器窗口"
          "会静默失败（2026-08-01 的自锁事故就是这个）")
    elif tt and not pp:
        w("能打字、Ctrl+V 不行 -> 组合键通道坏了（卡键或修饰键被吞）")
    elif not tt and not pp:
        w("打字和粘贴都没进去 -> 键盘 SendInput 整条被吞，或点的位置根本不可编辑")
        w("   注意：正文顶部 60px 以内是标题行，点那儿粘不进去（实测）")
    if (t2 is not None) and (not t1) and t2:
        w("**释放修饰键后恢复 -> 确认是修饰键卡死**")

    win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
    time.sleep(2.0)
    S._drop_topmost()
    w("done. 编辑器已关闭（收藏里可能留了含 DIAGTYPE/DIAGPASTE 的测试笔记）")


if __name__ == "__main__":
    main()
