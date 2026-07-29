# -*- coding: utf-8 -*-
"""只读 UIA 诊断：排查 ai_news_note 的 '笔记编辑器未打开'。

用法：python plugins/ai_news_note/diag_note.py [dump|fav]
  dump : 列出所有顶层窗口（class/name/rect）。零副作用，任意会话可跑。
  fav  : 额外点一下"收藏"标签，dump 左上区域的 XTableCell + 生产谓词
         实际匹配到哪个格子。只点"收藏"标签（可逆），不点笔记格子、
         不打开编辑器、不粘贴、不发送。仅在桌面会话(2)里有意义。

输出 UTF-8 写到 panel_logs/diag_note.log（同时 print）。
"""
import os
import sys
import time
import datetime

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "diag_note.log")


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


def _win_at(x, y):
    """报告屏幕点 (x,y) 当前被哪个顶层窗口占着（只读，不点击）。"""
    import win32gui
    try:
        h = win32gui.WindowFromPoint((int(x), int(y)))
        root = win32gui.GetAncestor(h, 2)  # GA_ROOT
        return f"hwnd={root} class={win32gui.GetClassName(root)!r} title={win32gui.GetWindowText(root)!r}"
    except Exception as e:
        return f"<probe err {e!r}>"


def _find_in(win, pred, max_depth=27):
    hit = [None]

    def f(c, d):
        if hit[0] or d > max_depth:
            return
        for ch in c.GetChildren():
            try:
                if pred(ch):
                    hit[0] = ch
                    return
            except Exception:
                pass
            f(ch, d + 1)

    f(win, 0)
    return hit[0]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dump"
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# diag_note mode={mode} @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from wxautox4.uia import uiautomation as auto
    root = auto.GetRootControl()

    w("== TOP-LEVEL WINDOWS ==")
    main_win = None
    for c in root.GetChildren():
        try:
            r = c.BoundingRectangle
            cls = c.ClassName
            nm = c.Name
            w(f"  class={cls!r} name={nm!r} rect=({r.left},{r.top},{r.right},{r.bottom})")
            if cls == "mmui::MainWindow" and main_win is None:
                main_win = c
        except Exception as e:
            w(f"  <err {e!r}>")

    if mode != "fav":
        w("(dump only; done)")
        return

    if not main_win:
        w("!! mmui::MainWindow 不在本会话 -> 这个会话的 UIA 看不到微信（多半是 session 0，需走 SWXRun 到会话 2）")
        return

    r = main_win.BoundingRectangle
    w(f"== MAIN WINDOW rect=({r.left},{r.top},{r.right},{r.bottom}) ==")

    # 复刻生产：先 SetForegroundWindow(主窗口)，再探针
    import win32gui
    try:
        win32gui.SetForegroundWindow(main_win.NativeWindowHandle)
        w(f"SetForegroundWindow(主窗口) 调用完毕；当前前台 hwnd={win32gui.GetForegroundWindow()}")
    except Exception as e:
        w(f"SetForegroundWindow 失败: {e!r}")
    time.sleep(0.6)

    fav = _find_in(main_win, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    if fav:
        fr = fav.BoundingRectangle
        cx, cy = (fr.left + fr.right) // 2, (fr.top + fr.bottom) // 2
        w(f"收藏 按钮: rect=({fr.left},{fr.top},{fr.right},{fr.bottom}) 中心({cx},{cy})")
        w(f"  ↳ 该点当前顶层窗口 = {_win_at(cx, cy)}")
        fav.Click(simulateMove=False)
    else:
        w("!! 收藏 按钮未找到")
    time.sleep(1.5)

    w("== 收藏点击后 左上区域 XTableCell (left<400, top<400) ==")
    cells = []

    def collect(c, d=0):
        if d > 27:
            return
        for ch in c.GetChildren():
            try:
                if ch.ClassName == "mmui::XTableCell":
                    rr = ch.BoundingRectangle
                    if rr.left < 400 and rr.top < 400:
                        cells.append((rr.top, rr.left, rr.right, rr.bottom, ch.Name or ""))
            except Exception:
                pass
            collect(ch, d + 1)

    collect(main_win)
    for t, l, rt, b, nm in sorted(cells):
        w(f"  cell top={t} left={l} right={rt} bottom={b} name={nm!r}")
    if not cells:
        w("  (无匹配 cell)")

    matched = _find_in(main_win, lambda c: (c.ClassName == "mmui::XTableCell" and (c.Name or "") == ""
                                            and c.BoundingRectangle.left < 309
                                            and 70 <= c.BoundingRectangle.top <= 95))
    if matched:
        mr = matched.BoundingRectangle
        cx, cy = (mr.left + mr.right) // 2, (mr.top + mr.bottom) // 2
        w(f"** 生产谓词匹配到: top={mr.top} left={mr.left} right={mr.right} bottom={mr.bottom} name={matched.Name!r} 中心({cx},{cy})")
        w(f"   ↳ 该点当前顶层窗口 = {_win_at(cx, cy)}")
        w("   (若上面不是 mmui::MainWindow，则生产代码的 cell.Click 会点到别的窗口 -> 编辑器不开)")
    else:
        w("** 生产谓词匹配到: 无 (生产代码会报 '新建笔记入口未找到')")
    w("done. (未点击笔记格子；未打开编辑器)")


if __name__ == "__main__":
    main()
