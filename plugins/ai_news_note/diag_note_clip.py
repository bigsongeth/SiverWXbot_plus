# -*- coding: utf-8 -*-
"""诊断第三轮：定位到 CF_HTML 富文本这一环。

前两轮的结论（别再重复走）：
  - 落点没问题：生产落点和正文控件中心都试过，行为一致。
  - 键盘没问题：普通打字、Ctrl+V、Ctrl+A/Ctrl+C 全部生效 —— 第二轮回读拿到了
    'DIAGTYPE123DIAGPASTE456DIAGTYPE123DIAGPASTE456'，两轮内容都真进了编辑器。
    （第二轮脚本里 UIA TextPattern 读到的是占位符 '￼'，那是读法不可靠，不是没粘进去。）
  - 于是生产和测试的唯一差别只剩一个：生产粘的是 CF_HTML 富文本（_clip_html），
    测试粘的是纯文本（_clip_text）。

本轮就验这一件事，用真实 latest.json 渲染出来的 HTML 复刻生产：
  1. _clip_html 写入后，立刻枚举剪贴板格式 —— HTML Format 到底在不在
  2. 等 2 秒再枚举一次 —— 有没有被别的程序（RustDesk 剪贴板同步是嫌疑）清掉或覆盖
  3. 富文本粘贴实测（回读用 Ctrl+A/Ctrl+C，已证实可靠）
  4. 对照组：同样内容改用纯文本粘贴，看是否成功

用法（会话 2）：python plugins/ai_news_note/diag_note_clip.py
副作用：同前两轮。输出 panel_logs/diag_note_clip.log。
"""
import os
import sys
import time
import json
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "panel_logs", "diag_note_clip.log")


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


def _dump_clip(tag):
    """枚举剪贴板里现有的格式，并报告 HTML/文本内容长度。"""
    import win32clipboard as wc
    opened = False
    for i in range(6):
        try:
            wc.OpenClipboard()
            opened = True
            break
        except Exception as e:
            w(f"[{tag}] OpenClipboard 第 {i+1} 次失败: {e!r}")
            time.sleep(0.5)
    if not opened:
        w(f"[{tag}] !! 剪贴板打不开（被别的进程锁着）")
        return None
    info = {}
    try:
        fmts = []
        f = 0
        while True:
            f = wc.EnumClipboardFormats(f)
            if f == 0:
                break
            try:
                name = wc.GetClipboardFormatName(f)
            except Exception:
                name = {1: "CF_TEXT", 13: "CF_UNICODETEXT", 16: "CF_LOCALE",
                        7: "CF_OEMTEXT"}.get(f, f"#{f}")
            fmts.append(f"{name}({f})")
        info["formats"] = fmts
        w(f"[{tag}] 剪贴板格式: {fmts}")
        try:
            CF_HTML = wc.RegisterClipboardFormat("HTML Format")
            raw = wc.GetClipboardData(CF_HTML)
            info["html_len"] = len(raw) if raw else 0
            head = (raw[:120] if isinstance(raw, (bytes, bytearray)) else str(raw)[:120])
            w(f"[{tag}] HTML Format 长度={info['html_len']} 开头={head!r}")
        except Exception as e:
            info["html_len"] = 0
            w(f"[{tag}] HTML Format 读不到: {e!r}")
        try:
            t = wc.GetClipboardData(wc.CF_UNICODETEXT)
            info["text"] = t
            w(f"[{tag}] CF_UNICODETEXT={str(t)[:80]!r}")
        except Exception as e:
            info["text"] = None
            w(f"[{tag}] CF_UNICODETEXT 读不到: {e!r}")
    finally:
        try:
            wc.CloseClipboard()
        except Exception:
            pass
    return info


def _paste_and_verify(S, cx, cy, expect, tag):
    """粘贴 + 用 Ctrl+A/Ctrl+C 回读校验（已证实可靠的验证方式）。"""
    from wxautox4.uia import uiautomation as auto
    auto.Click(cx, cy)
    time.sleep(1.2)
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}v", waitTime=0.1); time.sleep(2.5)
    S._clip_text("___CHK___"); time.sleep(0.4)
    auto.SendKeys("{Ctrl}a", waitTime=0.1); time.sleep(0.3)
    auto.SendKeys("{Ctrl}c", waitTime=0.1); time.sleep(1.3)
    back = S._clip_read()
    ok = bool(back) and expect in str(back)
    w(f"[{tag}] 粘贴后回读={str(back)[:80]!r}")
    w(f"[{tag}] 期望包含={expect!r} -> {'✅ 成功' if ok else '❌ 失败'}")
    return ok


def main():
    try:
        open(OUT, "w", encoding="utf-8").close()
    except Exception:
        pass
    w(f"# diag_note_clip @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")

    from wxautox4.uia import uiautomation as auto
    import win32gui
    import win32con
    from plugins.ai_news_note import sender as S
    from plugins.ai_news_note import config
    from plugins.ai_news_note.render import render

    ok, why = S._desktop_usable()
    w(f"桌面可用: {ok} {why}")
    if not ok:
        return

    # ---- 用真实数据渲染，完全复刻生产 ----
    if not os.path.exists(config.DATA_FILE):
        w(f"!! 数据文件不存在: {config.DATA_FILE}")
        return
    with open(config.DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    w(f"数据: date={d.get('date')} count={d.get('count')} generated_at={d.get('generated_at')}")
    title, frag, hits = render(d)
    cf_bytes = S._build_cf_html(frag)
    title_kw = title.replace("🐶", "").strip()
    w(f"标题={title!r} 校验串={title_kw!r}")
    w(f"HTML 片段长度={len(frag)} CF_HTML 字节数={len(cf_bytes)}")

    # ---- 1/2. 写剪贴板，立刻 + 延迟各查一次 ----
    w("== 写入 CF_HTML 后剪贴板状态 ==")
    try:
        S._clip_html(cf_bytes, title)
        w("_clip_html 调用成功（未抛异常）")
    except Exception as e:
        w(f"!! _clip_html 抛异常: {e!r}")
        return
    i1 = _dump_clip("写入后立刻")
    time.sleep(2.0)
    i2 = _dump_clip("写入后 2 秒")
    if i1 and i2:
        if i1.get("html_len") and not i2.get("html_len"):
            w("** HTML Format 在 2 秒内被清掉了 —— 有别的程序在改剪贴板（RustDesk 剪贴板同步？）")
        elif i1.get("text") != i2.get("text"):
            w("** 剪贴板文本 2 秒内被改了 —— 有别的程序在抢剪贴板")
        else:
            w("剪贴板 2 秒内稳定，没被别人动")

    # ---- 打开编辑器 ----
    S._close_update_windows()
    S._close_all_editors()
    wx = S._top("mmui::MainWindow")
    if not wx:
        w("!! 主窗口未找到")
        return
    hwnd = wx.NativeWindowHandle
    S._raise(wx)
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
    S._raise_hwnd(nh)
    w(f"编辑器 hwnd={nh} rect={r} 前台={win32gui.GetForegroundWindow()}")
    doc = S._find_in(note, lambda c: c.ControlTypeName == "DocumentControl")
    b = doc.BoundingRectangle if doc else r
    cx, cy = (b.left + b.right) // 2, (b.top + b.bottom) // 2

    # ---- 3. 富文本粘贴实测 ----
    w("== A. CF_HTML 富文本粘贴（生产走的这条）==")
    S._clip_html(cf_bytes, title)
    time.sleep(0.5)
    _dump_clip("粘贴前")
    ok_html = _paste_and_verify(S, cx, cy, title_kw, "CF_HTML")

    # ---- 4. 对照组：纯文本 ----
    w("== B. 对照组：同样标题走纯文本 ==")
    S._clip_text(title)
    time.sleep(0.5)
    ok_text = _paste_and_verify(S, cx, cy, title_kw, "纯文本")

    w("== 判定 ==")
    if ok_html:
        w("富文本粘贴成功 -> 这一轮复现不出来，问题是间歇性的（看上面剪贴板是否被抢）")
    elif ok_text:
        w("**富文本失败、纯文本成功 -> 确认是 CF_HTML 这一环坏的**")
        w("   往下查：HTML 体积、剪贴板被第三方改写、微信笔记编辑器对 HTML 粘贴的处理变化")
    else:
        w("两种都失败 -> 不是格式问题，回头看编辑器状态（是不是打开的不是可编辑的新笔记）")

    win32gui.PostMessage(nh, win32con.WM_CLOSE, 0, 0)
    time.sleep(2.0)
    S._drop_topmost()
    w("done. 编辑器已关闭")


if __name__ == "__main__":
    main()
