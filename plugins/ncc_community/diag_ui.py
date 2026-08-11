# -*- coding: utf-8 -*-
"""会话 2 的 UI 取证：列出微信可见窗口 + 截图，供远程排查转发/UI 卡死。

为什么必须跑在会话 2（由计划任务 SWXRun 触发）：
SSH 进来落在 session 0，EnumWindows 只能看到 session 0 的桌面，微信（session 2）
的窗口一个都看不见 —— 2026-08-04 排查时在这上面误判过"微信窗口全没了"。

为什么要截图：
在此之前，"屏幕上现在是什么样"只能靠人肉描述（"发送给对话框开着吗""搜索词在变吗"），
一来一回极慢且容易失真。截图落盘后可以直接取回来看，不再需要人配合。

输出：
  C:\\Users\\Admin\\swx_run_out.txt   窗口清单（由 swx_run.cmd 重定向）
  C:\\Users\\Admin\\ui_shot.png       全屏截图
"""
import datetime
import os

import psutil
import win32con
import win32gui
import win32process
import win32ui

SHOT_PATH = r"C:\Users\Admin\ui_shot.png"

WX_PROCS = ("weixin.exe", "wechat.exe")


def wechat_windows():
    rows = []

    def cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return
            _, pid = win32process.GetWindowThreadProcessId(h)
            if psutil.Process(pid).name().lower() not in WX_PROCS:
                return
            rows.append((h, win32gui.GetClassName(h), win32gui.GetWindowText(h),
                         win32gui.GetWindowRect(h)))
        except Exception:
            return

    win32gui.EnumWindows(cb, None)
    return rows


def grab_screen(path=SHOT_PATH):
    """整屏截图。用 win32 原生 BitBlt，不依赖 Pillow（这台机器上不一定装了）。"""
    w = win32api_metrics(win32con.SM_CXVIRTUALSCREEN)
    h = win32api_metrics(win32con.SM_CYVIRTUALSCREEN)
    x = win32api_metrics(win32con.SM_XVIRTUALSCREEN)
    y = win32api_metrics(win32con.SM_YVIRTUALSCREEN)
    desktop = win32gui.GetDesktopWindow()
    src_dc = win32gui.GetWindowDC(desktop)
    src = win32ui.CreateDCFromHandle(src_dc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    mem.BitBlt((0, 0), (w, h), src, (x, y), win32con.SRCCOPY)
    bmp.SaveBitmapFile(mem, path.replace(".png", ".bmp"))
    mem.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(desktop, src_dc)
    win32gui.DeleteObject(bmp.GetHandle())
    return path.replace(".png", ".bmp"), w, h


def win32api_metrics(idx):
    import win32api
    return win32api.GetSystemMetrics(idx)


def main():
    print("TIME:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 前台窗口是谁 —— 抢焦点类故障看这个
    try:
        fg = win32gui.GetForegroundWindow()
        _, fpid = win32process.GetWindowThreadProcessId(fg)
        print("FOREGROUND: %r proc=%s" % (win32gui.GetWindowText(fg),
                                          psutil.Process(fpid).name()))
    except Exception as e:
        print("FOREGROUND: <读不到> %s" % e)

    rows = wechat_windows()
    print("WECHAT VISIBLE TOP WINDOWS:", len(rows))
    for h, cls, title, rect in rows:
        print("  hwnd=%-10d cls=%-24r title=%-20r rect=%s" % (h, cls, title, rect))

    try:
        path, w, h = grab_screen()
        print("SCREENSHOT: %s (%dx%d, %d bytes)" % (path, w, h, os.path.getsize(path)))
    except Exception as e:
        print("SCREENSHOT FAILED: %r" % (e,))


if __name__ == "__main__":
    main()
