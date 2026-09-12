# -*- coding: utf-8 -*-
"""新形态（内嵌侧边栏）下，走一遍完整的建笔记链路，验证每一步。
会话 2 跑，跑之前请先停掉机器人线程。"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import win32gui, win32con
from wxautox4.uia import uiautomation as auto
from plugins.ai_news_note import sender as S


def find_in(root, pred, maxd=27):
    def f(c, d):
        if d > maxd:
            return None
        try:
            kids = c.GetChildren()
        except Exception:
            return None
        for k in kids:
            try:
                if pred(k):
                    return k
            except Exception:
                pass
            r = f(k, d + 1)
            if r:
                return r
        return None
    return f(root, 0)


def find_editor(wx):
    """兼容两种形态找笔记编辑器。返回 (host, doc, hwnd, mode)。"""
    for wd in list(auto.GetRootControl().GetChildren()):
        try:
            if wd.ClassName == "Chrome_WidgetWin_0" and (wd.Name or "") == "笔记":
                doc = find_in(wd, lambda c: c.ControlTypeName == "DocumentControl")
                return wd, doc, wd.NativeWindowHandle, "window"
        except Exception:
            pass
    host = find_in(wx, lambda c: (c.ClassName == "Chrome_WidgetWin_0"
                                  and c.ControlTypeName == "PaneControl"
                                  and c.NativeWindowHandle))
    if host:
        doc = find_in(host, lambda c: (c.ControlTypeName == "DocumentControl"
                                       and (c.Name or "") == "笔记"))
        if doc:
            return host, doc, host.NativeWindowHandle, "panel"
        return host, None, host.NativeWindowHandle, "panel-nodoc"
    return None, None, 0, ""


def top_fav_titles(wx, n=3):
    """收藏列表最上面几条的标题，用来确认新笔记有没有入库。"""
    wb = wx.BoundingRectangle
    out = []
    def walk(c, d=0):
        if d > 27 or len(out) >= n:
            return
        try:
            kids = c.GetChildren()
        except Exception:
            return
        for k in kids:
            try:
                if k.ClassName == "mmui::XTableCell":
                    b = k.BoundingRectangle
                    if b.left - wb.left > 290 and (b.bottom - b.top) > 100:
                        out.append((k.Name or '')[:40])
                        if len(out) >= n:
                            return
            except Exception:
                pass
            walk(k, d + 1)
    walk(wx)
    return out


def count_tabs(host):
    if not host:
        return -1
    n = [0]
    def walk(c, d=0):
        if d > 12:
            return
        try:
            kids = c.GetChildren()
        except Exception:
            return
        for k in kids:
            try:
                if k.ClassName == "Tab":
                    n[0] += 1
            except Exception:
                pass
            walk(k, d + 1)
    walk(host)
    return n[0]


def main():
    stamp = time.strftime('%H%M%S')
    title = f"肥肉诊断测试 {time.strftime('%m月%d日')} {stamp}"
    html = f"<div><h2>{title}</h2><p>这是排查笔记侧边栏改版的测试笔记，可以删除。</p></div>"
    plain = f"{title}\n这是排查笔记侧边栏改版的测试笔记，可以删除。"
    print(f"=== diag_note_flow {time.strftime('%Y-%m-%d %H:%M:%S')} 标题={title!r} ===")

    wx = None
    for w in list(auto.GetRootControl().GetChildren()):
        try:
            if w.ClassName == "mmui::MainWindow":
                wx = w
                break
        except Exception:
            pass
    if not wx:
        print("没找到主窗口"); return
    wx.SetActive(); time.sleep(1.0)
    wb = wx.BoundingRectangle
    print(f"主窗口 hwnd={wx.NativeWindowHandle} rect=({wb.left},{wb.top},{wb.right},{wb.bottom})")

    h0, d0, hw0, m0 = find_editor(wx)
    print(f"[开局] 编辑器 mode={m0!r} hwnd={hw0} tabs={count_tabs(h0)}")

    fav = find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    b = fav.BoundingRectangle
    auto.Click((b.left + b.right) // 2, (b.top + b.bottom) // 2)
    time.sleep(2.0)
    print(f"[收藏页] 顶部三条：{top_fav_titles(wx)}")

    cell = find_in(wx, lambda c: (c.ClassName == "mmui::XTableCell" and (c.Name or "") == ""
                                  and 40 < c.BoundingRectangle.left - wb.left < 130
                                  and c.BoundingRectangle.right - wb.left < 330
                                  and 55 <= c.BoundingRectangle.top - wb.top <= 115
                                  and c.BoundingRectangle.bottom - c.BoundingRectangle.top > 30))
    if not cell:
        print("!!! 没找到新建笔记入口"); return
    tb = cell.BoundingRectangle
    print(f">>> 点新建笔记 ({(tb.left+tb.right)//2},{(tb.top+tb.bottom)//2})")
    auto.Click((tb.left + tb.right) // 2, (tb.top + tb.bottom) // 2)

    host = doc = None
    mode = ""
    for i in range(16):
        time.sleep(0.5)
        host, doc, hw, mode = find_editor(wx)
        if doc:
            print(f"[找到编辑器] +{(i+1)*0.5}s mode={mode} hwnd={hw} "
                  f"doc_rect={doc.BoundingRectangle} tabs={count_tabs(host)}")
            break
    if not doc:
        print(f"!!! 没找到编辑器正文（mode={mode}）"); return

    fg = win32gui.GetForegroundWindow()
    print(f"前台={fg} 编辑器宿主hwnd={hw} 相等={fg == hw}")

    db = doc.BoundingRectangle
    cx, cy = (db.left + db.right) // 2, (db.top + db.bottom) // 2
    print(f"落点=({cx},{cy}) 该点归属={win32gui.WindowFromPoint((cx, cy))} "
          f"class={win32gui.GetClassName(win32gui.WindowFromPoint((cx, cy)))}")

    # 粘贴前先看正文是不是空的（防止把一条已有的旧笔记覆盖掉）
    auto.Click(cx, cy); time.sleep(1.2)
    S._clip_text("___EMPTY_CHK___"); time.sleep(0.3)
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.0)
    pre = S._clip_read()
    print(f"[粘贴前正文回读] {str(pre)[:80]!r}")

    ok = False
    for attempt in range(1, 4):
        S._clip_html(S._build_cf_html(html), plain)
        auto.Click(cx, cy); time.sleep(1.5)
        auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
        auto.SendKeys("{Ctrl}v", waitTime=0.1); time.sleep(2.5)
        S._clip_text("___CHK___"); time.sleep(0.4)
        auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
        auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
        back = S._clip_read()
        if back and title in str(back):
            ok = True
            print(f"[粘贴校验] 第 {attempt} 次通过")
            break
        print(f"[粘贴校验] 第 {attempt} 次未过，读回={str(back)[:60]!r}")
    if not ok:
        print("!!! 粘贴没进去"); return

    print(f"\n>>> 关闭编辑器（PostMessage WM_CLOSE -> {hw}）")
    win32gui.PostMessage(hw, win32con.WM_CLOSE, 0, 0)
    time.sleep(3.0)
    h2, d2, hw2, m2 = find_editor(wx)
    print(f"[关闭后] mode={m2!r} hwnd={hw2} doc={'有' if d2 else '无'} tabs={count_tabs(h2)}")

    print(f"[收藏列表] 顶部三条：{top_fav_titles(wx)}")
    hit = S._find_note_cell(title.replace('肥肉诊断测试 ', ''))
    print(f"[按标题找收藏格] {hit}")
    print("\n=== 结束 ===")


main()
