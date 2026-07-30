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


def confirm_group_window(wx, group_name: str) -> tuple[bool, str]:
    """确认主窗口此刻真的停在 group_name 这个群上。返回 (是否确认, 说明)。

    为什么必须确认：ChatWith 找不到会话是静默失败（返回 falsy，不抛异常），而这里紧接着
    要 SetGroupRemark——切歪了就会把「原名🐶」打到别的群头上，而 wxautox 的备注是
    【追加且清不掉】的，属于不可逆误操作（2026-07-30 排查拉群故障时发现的同类隐患）。
    用 exact=False 模糊搜索时更要防：模糊命中的可能是另一个名字相近的群。
    读不到窗口信息一律【判为未确认】——打备注不可逆，宁可这次不打（批量指令可以重来）。"""
    try:
        info = wx.ChatInfo() or {}
    except Exception as e:
        log("WARNING", f"复核当前聊天窗口失败，不打备注 {group_name}: {e}")
        return False, f"读不到当前窗口信息（{e}）"
    if not isinstance(info, dict) or not info:
        return False, "读不到当前窗口信息"
    chat_type = str(info.get("chat_type") or "")
    if chat_type != "group":
        return False, f"当前窗口不是群聊（chat_type={chat_type or '未知'}）"
    name = str(info.get("chat_name") or "").strip()
    rmk = str(info.get("remark") or "").strip()
    want = group_name.strip()
    if not (name or rmk):
        return False, "读不到当前窗口的会话名"
    if want in (name, rmk) or want in (name.rstrip(DOG), rmk.rstrip(DOG)):
        return True, ""
    return False, f"当前窗口是「{name or rmk}」，不是「{group_name}」"


def apply_remark(wx, group_name: str) -> tuple[bool, str]:
    """给一个群打上「原名🐶」备注。登记表标志幂等：已打过直接返回 True。
    返回 (是否成功, 说明)。调用方须在 MAIN_WINDOW_LOCK 内调用。"""
    data = registry.load()
    g = registry.get_group(data, group_name)
    if g and g.get("remark_applied"):
        return True, "已打过备注，跳过"

    remark = group_name + DOG
    try:
        sw = wx.ChatWith(group_name, exact=False)
        ok, why = confirm_group_window(wx, group_name)
        if not ok:
            # 切群失败/切歪了就别动备注（打错的备注清不掉）
            log("WARNING", f"打备注前切群没确认成功 {group_name}: {why}")
            return False, f"切到该群失败（{why}）"
        if not (sw is None or sw):
            log("WARNING", f"打备注前 ChatWith 返回失败但窗口已确认，继续 {group_name}")
        r = wx.SetGroupRemark(remark)
    except Exception as e:
        log("ERROR", f"打备注失败 {group_name}: {e}")
        return False, str(e)

    if wxresponse_ok(r):
        registry.mark_remark_applied(group_name, remark)
        log("INFO", f"已打🐶备注：{group_name} -> {remark}")
        return True, remark
    return False, f"SetGroupRemark 返回 {r!r}"
