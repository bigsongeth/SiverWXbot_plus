# -*- coding: utf-8 -*-
"""诊断：ai_news_note 粘贴失败（校验读回 ___CHK___）到底卡在哪。

背景：2026-07-30 之后日报再没发出去，失败点固定在
`_create_note_from_clipboard` 的粘贴校验——前台窗口是对的、鼠标点也落在
编辑器渲染区，但 Ctrl+A/Ctrl+C 读回来的还是哨兵串，说明 Ctrl+V 落空了。
怀疑 sender.py 里 `cy = r.top + 130` 这个魔数在新分辨率下不再指向正文
（主窗口高度 07-30 是 909、07-31 起是 1040）。

用法（必须在有桌面的会话 2 里跑，SSH 的 session 0 看不到微信窗口）：
    python plugins/ai_news_note/diag_note_editor.py          # 只 dump 布局，不按任何键
    python plugins/ai_news_note/diag_note_editor.py paste    # 额外做粘贴实测（推荐）

paste 模式做三件事：
  A. 在生产落点 (中心, top+130) 复刻一次粘贴校验，用短文本
  B. 若 A 失败，从 UIA 子树里挑出真正的正文控件，在它中心再试一次
  C. 报告两次结果

⚠️ 2026-08-01 的教训，看结果前必读：A 和 B 是在**同一个编辑器窗口**里连着测的，
A 失败会把焦点/选区搞脏，B 跟着失败 —— 于是「两次都败」会被误读成「键盘整条不通」，
我就是这么白绕了两轮。要判落点，请用 diag_note_spots.py，它每个落点都开一个全新编辑器。
本脚本的价值在于 dump 布局（编辑器 rect、正文控件 rect、落点归属），判定结论别尽信。

副作用：抢一次微信前台约 10-20 秒，开一个笔记编辑器，结束时 WM_CLOSE 关掉
（微信可能因此在收藏里留一条测试笔记，手动删掉即可）。不发任何消息、不改配置。
输出 UTF-8 写到 panel_logs/diag_note_editor.log。
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "diag_note_editor.log")

_PASTE_TEXT = "DIAG_NOTE_PROBE_20260801_ABCDEF"


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


def _rect(c):
    try:
        r = c.BoundingRectangle
        return f"({r.left},{r.top},{r.right},{r.bottom})[{r.right-r.left}x{r.bottom-r.top}]"
    except Exception as e:
        return f"<rect err {e!r}>"


def _probe_point(x, y):
    """报告屏幕点 (x,y) 上的 Win32 窗口 + UIA 控件（只读，不点击）。"""
    import win32gui
    from wxautox4.uia import uiautomation as auto
    out = []
    try:
        h = win32gui.WindowFromPoint((int(x), int(y)))
        root = win32gui.GetAncestor(h, 2)
        out.append(f"win32: hwnd={h} class={win32gui.GetClassName(h)!r} "
                   f"root={root}({win32gui.GetClassName(root)!r})")
    except Exception as e:
        out.append(f"win32: <err {e!r}>")
    try:
        c = auto.ControlFromPoint(int(x), int(y))
        if c:
            out.append(f"uia:   type={c.ControlTypeName} class={c.ClassName!r} "
                       f"name={(c.Name or '')[:40]!r} rect={_rect(c)}")
        else:
            out.append("uia:   <None>")
    except Exception as e:
        out.append(f"uia:   <err {e!r}>")
    return out


def _walk(win, max_depth=6, limit=150):
    """dump 子树，返回 [(depth, ctrl)]。"""
    got = []

    def f(c, d):
        if d > max_depth or len(got) >= limit:
            return
        try:
            children = c.GetChildren()
        except Exception:
            return
        for ch in children:
            if len(got) >= limit:
                return
            got.append((d, ch))
            f(ch, d + 1)

    f(win, 0)
    return got


def _paste_check(S, nh, cx, cy, label):
    """复刻生产的粘贴 + 硬校验，返回 (ok, 读回内容)。"""
    from wxautox4.uia import uiautomation as auto
    import win32gui
    S._clip_text(_PASTE_TEXT)
    time.sleep(0.4)
    auto.Click(cx, cy)
    time.sleep(1.8)
    fg = win32gui.GetForegroundWindow()
    w(f"  [{label}] 点击 ({cx},{cy}) 后：前台={fg}"
      f"({win32gui.GetClassName(fg) if fg else ''}) 期望={nh} "
      f"点归属编辑器={S._owns_point(nh, cx, cy)}")
    for line in _probe_point(cx, cy):
        w(f"    落点 {line}")
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}v", waitTime=0.1); time.sleep(2.0)
    S._clip_text("___CHK___"); time.sleep(0.4)
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
    back = S._clip_read()
    ok = bool(back) and _PASTE_TEXT in str(back)
    w(f"  [{label}] 读回={str(back)[:60]!r} -> {'✅ 粘进去了' if ok else '❌ 没粘进去'}")
    return ok, back


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "layout"
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# diag_note_editor mode={mode} @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from wxautox4.uia import uiautomation as auto
    import win32gui
    import win32con
    from plugins.ai_news_note import sender as S

    # ---- 0. 环境 ----
    try:
        import ctypes
        u = ctypes.windll.user32
        w(f"屏幕: {u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)} "
          f"虚拟桌面: {u.GetSystemMetrics(78)}x{u.GetSystemMetrics(79)}")
    except Exception as e:
        w(f"屏幕尺寸取不到: {e!r}")
    usable, why = S._desktop_usable()
    w(f"桌面可用: {usable} {why}")
    if not usable:
        w("!! 会话锁屏/断开，后面全是瞎跑，先解锁再来")
        return

    w("== 顶层窗口 ==")
    for c in auto.GetRootControl().GetChildren():
        try:
            w(f"  class={c.ClassName!r} name={(c.Name or '')[:30]!r} rect={_rect(c)}")
        except Exception as e:
            w(f"  <err {e!r}>")

    S._close_update_windows()
    S._close_all_editors()

    # ---- 1. 主窗口 -> 收藏 -> 新建笔记（完全复刻生产）----
    wx = S._top("mmui::MainWindow")
    if not wx:
        w("!! 主窗口未找到（多半是在 session 0 跑的，UIA 看不到微信）")
        return
    hwnd = wx.NativeWindowHandle
    w(f"主窗口 rect={_rect(wx)} 置前={S._raise(wx)}")
    wb = wx.BoundingRectangle

    fav = S._find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    if not fav:
        w("!! '收藏' 入口未找到")
        return
    w(f"'收藏' 按钮 rect={_rect(fav)} 点击={S._click(fav, hwnd, '收藏')}")
    time.sleep(1.6)

    w("== 收藏页左上区 XTableCell（相对主窗口坐标）==")
    for d, c in _walk(wx, max_depth=27, limit=4000):
        try:
            if c.ClassName != "mmui::XTableCell":
                continue
            b = c.BoundingRectangle
            rl, rt = b.left - wb.left, b.top - wb.top
            if rl < 400 and rt < 400:
                w(f"  rel_left={rl} rel_top={rt} rel_right={b.right-wb.left} "
                  f"h={b.bottom-b.top} name={(c.Name or '')[:30]!r}")
        except Exception:
            pass

    def _is_new_note(c):
        if c.ClassName != "mmui::XTableCell" or (c.Name or "") != "":
            return False
        b = c.BoundingRectangle
        return (40 < b.left - wb.left < 130
                and b.right - wb.left < 330
                and 55 <= b.top - wb.top <= 115
                and b.bottom - b.top > 30)

    cell = S._find_in(wx, _is_new_note)
    if not cell:
        w("!! 生产谓词没匹配到「新建笔记」格子 -> 生产会报 '新建笔记入口未找到'")
        return
    w(f"生产谓词匹配到新建笔记格 rect={_rect(cell)} 点击={S._click(cell, hwnd, '新建笔记')}")
    time.sleep(3.0)

    # ---- 2. 编辑器布局 ----
    note = S._top("Chrome_WidgetWin_0", "笔记")
    if not note:
        w("!! 笔记编辑器未打开")
        return
    r = note.BoundingRectangle
    nh = note.NativeWindowHandle
    w(f"== 笔记编辑器 hwnd={nh} rect={_rect(note)} ==")
    got_fg = S._raise_hwnd(nh)
    w(f"编辑器置前={got_fg} 当前前台={win32gui.GetForegroundWindow()}")

    prod_cx, prod_cy = (r.left + r.right) // 2, r.top + 130
    w(f"生产落点 = 中心x={prod_cx}, y=top+130={prod_cy}（相对编辑器顶部 130px）")
    for line in _probe_point(prod_cx, prod_cy):
        w(f"  {line}")

    w("== 编辑器 UIA 子树（深度<=6，相对编辑器窗口坐标）==")
    cands = []
    for d, c in _walk(note, max_depth=6, limit=150):
        try:
            b = c.BoundingRectangle
            tn = c.ControlTypeName
            w(f"  {'  '*d}[{d}] {tn} class={c.ClassName!r} name={(c.Name or '')[:28]!r} "
              f"rel=({b.left-r.left},{b.top-r.top},{b.right-r.left},{b.bottom-r.top}) "
              f"[{b.right-b.left}x{b.bottom-b.top}]")
            if tn in ("DocumentControl", "EditControl", "TextControl") and \
                    (b.right - b.left) > 200 and (b.bottom - b.top) > 100:
                cands.append((d, tn, c, b))
        except Exception:
            pass

    w("== 正文候选控件（Document/Edit 且够大）==")
    for d, tn, c, b in cands:
        w(f"  [{d}] {tn} rel_top={b.top-r.top} 中心=({(b.left+b.right)//2},{(b.top+b.bottom)//2}) "
          f"rect={_rect(c)}")
    if not cands:
        w("  (没找到 —— Chromium 的 accessibility 树可能没展开，只能靠坐标试)")

    if mode != "paste":
        w("layout 模式结束，关闭编辑器（不粘贴、不保存内容）")
        win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
        time.sleep(2.0)
        S._drop_topmost()
        return

    # ---- 3. A：生产落点粘贴实测 ----
    w("== A. 生产落点粘贴实测 ==")
    ok_a, _ = _paste_check(S, nh, prod_cx, prod_cy, "生产落点 top+130")

    # ---- 4. B：正文控件中心再试 ----
    ok_b = None
    if not ok_a:
        target = None
        if cands:
            # 取最大的那个当正文
            target = max(cands, key=lambda t: (t[3].right - t[3].left) * (t[3].bottom - t[3].top))
        if target:
            b = target[3]
            bx, by = (b.left + b.right) // 2, (b.top + b.bottom) // 2
            w(f"== B. 改点正文控件 {target[1]} 中心 ({bx},{by}) 再试 ==")
            ok_b, _ = _paste_check(S, nh, bx, by, "正文控件中心")
        else:
            # 没有 UIA 候选就扫几个相对高度，看哪个能粘进去
            w("== B. 无 UIA 候选，按相对高度扫点 ==")
            for dy in (200, 260, 320, 400):
                cy2 = r.top + dy
                if cy2 >= r.bottom - 20:
                    continue
                ok, _ = _paste_check(S, nh, prod_cx, cy2, f"top+{dy}")
                if ok:
                    ok_b = True
                    w(f"** top+{dy} 能粘进去 -> 生产的 130 应该改成 {dy} 附近")
                    break

    w("== 判定 ==")
    if ok_a:
        w("A 成功：落点没问题，粘贴链路本身是通的 —— 失败另有原因（时序/剪贴板被抢？）")
    elif ok_b:
        w("A 败 B 成：**确认是落点问题**，sender.py:346 的 r.top+130 在当前分辨率下没指到正文")
    else:
        w("A B 都败：不是落点问题，是键盘输入/焦点根本没进 Chromium —— 往 SendKeys/会话输入方向查")

    win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
    time.sleep(2.0)
    S._drop_topmost()
    w("done. 编辑器已关闭（收藏里可能留了一条含 DIAG_NOTE_PROBE 的测试笔记，手动删掉）")


if __name__ == "__main__":
    main()
