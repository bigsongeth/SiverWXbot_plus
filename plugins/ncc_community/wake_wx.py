# -*- coding: utf-8 -*-
"""把关进托盘的微信主窗口唤回来（会话 2 专用）。

restore_wx.ps1 只处理【可见但最小化】的窗口，而主窗口一旦关到托盘就是【不可见】，
被它的 IsWindowVisible 过滤掉了 —— 2026-08-11 实测：子窗口全恢复了，主窗口还是没有，
于是 wxautox 的每一次 UI 操作都报 Find Control Timeout: EditControl。

这里枚举微信进程的【全部】顶层窗口（含不可见），只对标题是 微信/Weixin/WeChat 的
主窗口做 SW_RESTORE + 置前，不碰其它窗口。
"""
import time

import psutil
import win32con
import win32gui
import win32process

MAIN_TITLES = ("微信", "Weixin", "WeChat")
WX_PROCS = ("weixin.exe", "wechat.exe")


def main():
    targets = []

    def cb(h, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if psutil.Process(pid).name().lower() not in WX_PROCS:
                return
            title = win32gui.GetWindowText(h)
            if title in MAIN_TITLES:
                targets.append((h, title, win32gui.IsWindowVisible(h)))
        except Exception:
            return

    win32gui.EnumWindows(cb, None)
    print("找到主窗口候选:", targets)

    for h, title, visible in targets:
        try:
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
            time.sleep(0.3)
            win32gui.ShowWindow(h, win32con.SW_SHOW)
            try:
                win32gui.SetForegroundWindow(h)
            except Exception as e:
                print("  置前失败(不致命):", e)
            time.sleep(0.5)
            print("  已唤起 hwnd=%d %r visible_before=%s -> now=%s rect=%s"
                  % (h, title, visible, win32gui.IsWindowVisible(h),
                     win32gui.GetWindowRect(h)))
        except Exception as e:
            print("  唤起失败 hwnd=%d: %r" % (h, e))

    if not targets:
        print("没有找到标题为 微信/Weixin/WeChat 的窗口 —— 主窗口可能已被彻底关闭，"
              "需要人在屏幕上点一下托盘图标")


if __name__ == "__main__":
    main()
