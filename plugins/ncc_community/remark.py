# -*- coding: utf-8 -*-
"""干净地给群打🐶备注 —— 引擎的③。

wxautox4 的 SetGroupRemark 对【已有备注】是追加而非替换、空串也清不掉
（2026-07-07 实测踩坑）。对策：
- 只对【没打过备注】的群设置一次（登记表 remark_applied 标志兜底），单次设置是干净的；
- 设置成功后记 remark_applied=True，永不重设 → 从根上避免追加。
需要纠正一个已打错的备注时，只能人工在微信里清空后，把登记表标志复位再重设。

所有微信操作走 bot.wx（机器人自己的 WeChat 实例，同进程），不再用独立脚本，
避免多实例抢窗口。调用方需持有 MAIN_WINDOW_LOCK。
"""
from __future__ import annotations

from . import registry
from .common import log

DOG = "\U0001f436"  # 🐶


def wxresponse_ok(r) -> bool:
    """SetGroupRemark 返回 {'status':'成功'|...} 或 WxResponse；宽松判成功。"""
    if r is None:
        return True
    if isinstance(r, dict):
        return r.get("status") in ("成功", "success", True) or bool(r.get("data")) or r.get("message") in (None, "")
    return bool(r)


def apply_remark(wx, group_name: str) -> tuple[bool, str]:
    """给一个群打上「原名🐶」备注。登记表标志幂等：已打过直接返回 True。
    返回 (是否成功, 说明)。调用方须在 MAIN_WINDOW_LOCK 内调用。"""
    data = registry.load()
    g = registry.get_group(data, group_name)
    if g and g.get("remark_applied"):
        return True, "已打过备注，跳过"

    remark = group_name + DOG
    try:
        wx.ChatWith(group_name, exact=False)
        r = wx.SetGroupRemark(remark)
    except Exception as e:
        log("ERROR", f"打备注失败 {group_name}: {e}")
        return False, str(e)

    if wxresponse_ok(r):
        registry.mark_remark_applied(group_name, remark)
        log("INFO", f"已打🐶备注：{group_name} -> {remark}")
        return True, remark
    return False, f"SetGroupRemark 返回 {r!r}"
