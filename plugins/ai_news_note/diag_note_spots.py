# -*- coding: utf-8 -*-
"""诊断第四轮：干净环境下逐个落点做对照，定死 sender.py 的 cy 该取哪。

为什么要重做对照（前三轮的教训）：
  - 第一轮把「生产落点」和「正文控件中心」放在同一个编辑器里连着测，A 先失败之后
    编辑器焦点已经脏了，B 跟着失败 -> 误判成「键盘整条不通」。
  - 第二轮证明键盘是通的（回读拿到了两轮累积的探针文本）。
  - 第三轮在全新编辑器里点正文控件中心，用今天真实日报的 CF_HTML 一次粘贴成功。
  => 所以每个落点必须各自开一个全新编辑器，测完就关，绝不复用。

本轮对每个候选落点独立跑一遍完整流程（关编辑器 -> 收藏 -> 新建笔记 -> 点 -> 粘贴 -> 回读校验），
用真实日报 CF_HTML，重复 REPEATS 轮取稳定结论。

用法（会话 2）：python plugins/ai_news_note/diag_note_spots.py
副作用：每个落点开关一次笔记编辑器，收藏里可能留下若干条测试笔记（标题就是当天日报标题，
需要手动删）。不发送任何消息。输出 panel_logs/diag_note_spots.log。
"""
import os
import sys
import time
import json
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "diag_note_spots.log")

REPEATS = 2


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


def _open_fresh_note(S, auto):
    """关掉所有编辑器，重新点收藏 -> 新建笔记，返回 (note, doc) 或 (None, None)。"""
    import win32gui
    S._close_update_windows()
    S._close_all_editors()
    wx = S._top("mmui::MainWindow")
    if not wx:
        w("  !! 主窗口未找到")
        return None, None
    hwnd = wx.NativeWindowHandle
    S._raise(wx)
    wb = wx.BoundingRectangle
    fav = S._find_in(wx, lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "收藏")
    if not fav:
        w("  !! '收藏' 未找到")
        return None, None
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
        w("  !! 新建笔记入口未找到")
        return None, None
    S._click(cell, hwnd, "新建笔记")
    time.sleep(3.0)
    note = S._top("Chrome_WidgetWin_0", "笔记")
    if not note:
        w("  !! 笔记编辑器未打开")
        return None, None
    S._raise_hwnd(note.NativeWindowHandle)
    doc = S._find_in(note, lambda c: c.ControlTypeName == "DocumentControl")
    return note, doc


def _test_spot(S, auto, cf_bytes, title, expect, spot_name, spot_fn):
    """在全新编辑器里测一个落点。spot_fn(note_rect, doc_rect) -> (x, y)。"""
    import win32gui
    import win32con
    note, doc = _open_fresh_note(S, auto)
    if not note:
        return None
    r = note.BoundingRectangle
    dr = doc.BoundingRectangle if doc else r
    x, y = spot_fn(r, dr)
    S._clip_html(cf_bytes, title)
    time.sleep(0.5)
    auto.Click(x, y)
    time.sleep(1.5)
    fg = win32gui.GetForegroundWindow()
    nh = note.NativeWindowHandle
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}v", waitTime=0.1); time.sleep(2.5)
    S._clip_text("___CHK___"); time.sleep(0.4)
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
    back = S._clip_read()
    ok = bool(back) and expect in str(back)
    w(f"  [{spot_name}] 点({x},{y}) 编辑器rect={r} 相对编辑器顶部={y-r.top}px "
      f"相对正文顶部={y-dr.top}px 前台对={fg == nh}")
    w(f"  [{spot_name}] 回读={str(back)[:60]!r} -> {'✅ 成功' if ok else '❌ 失败'}")
    win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
    time.sleep(2.0)
    return ok


def main():
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# diag_note_spots @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from wxautox4.uia import uiautomation as auto
    from plugins.ai_news_note import sender as S
    from plugins.ai_news_note import config
    from plugins.ai_news_note.render import render

    ok, why = S._desktop_usable()
    w(f"桌面可用: {ok} {why}")
    if not ok:
        return

    with open(config.DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    title, frag, hits = render(d)
    cf_bytes = S._build_cf_html(frag)
    expect = title.replace("🐶", "").strip()
    w(f"用真实日报：{title!r} CF_HTML={len(cf_bytes)}B 校验串={expect!r}")

    spots = [
        ("生产 top+130", lambda r, dr: ((r.left + r.right) // 2, r.top + 130)),
        ("正文控件中心", lambda r, dr: ((dr.left + dr.right) // 2, (dr.top + dr.bottom) // 2)),
        ("正文顶部+60", lambda r, dr: ((dr.left + dr.right) // 2, dr.top + 60)),
        ("正文顶部+150", lambda r, dr: ((dr.left + dr.right) // 2, dr.top + 150)),
    ]

    results = {}
    for rnd in range(1, REPEATS + 1):
        w(f"===== 第 {rnd} 轮 =====")
        for name, fn in spots:
            res = _test_spot(S, auto, cf_bytes, title, expect, name, fn)
            results.setdefault(name, []).append(res)

    w("== 汇总 ==")
    for name, rs in results.items():
        good = sum(1 for x in rs if x)
        w(f"  {name}: {good}/{len(rs)} 成功  明细={rs}")

    prod = results.get("生产 top+130", [])
    body = results.get("正文控件中心", [])
    if prod and body and not any(prod) and all(body):
        w("**确认：生产的 r.top+130 落点已失效，正文控件中心稳定可用**")
        w("   修法：cy 改为按 DocumentControl 的实际 rect 取中心，别再用窗口顶部固定偏移")
    elif any(prod):
        w("生产落点这轮还能成 -> 是间歇性问题，别急着改坐标")

    S._drop_topmost()
    w("done.")


if __name__ == "__main__":
    main()
