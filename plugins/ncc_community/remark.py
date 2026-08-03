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


def _read_info(wx, group_name: str):
    """读当前窗口信息。返回 (info, 错误说明)，读不到时 info 为 None。"""
    try:
        info = wx.ChatInfo() or {}
    except Exception as e:
        log("WARNING", f"复核当前聊天窗口失败 {group_name}: {e}")
        return None, f"读不到当前窗口信息（{e}）"
    if not isinstance(info, dict) or not info:
        return None, "读不到当前窗口信息"
    return info, ""


def confirm_group_window(wx, group_name: str, expect_remark: str = "") -> tuple[bool, str]:
    """确认主窗口此刻真的停在 group_name 这个群上，【且该群还没有备注】。
    返回 (是否确认, 说明)。

    为什么必须确认：ChatWith 找不到会话是静默失败（返回 falsy，不抛异常），而这里紧接着
    要 SetGroupRemark——切歪了就会把「原名🐶」打到别的群头上，而 wxautox 的备注是
    【追加且清不掉】的，属于不可逆误操作（2026-07-30 排查拉群故障时发现的同类隐患）。
    用 exact=False 模糊搜索时更要防：模糊命中的可能是另一个名字相近的群。
    读不到窗口信息一律【判为未确认】——打备注不可逆，宁可这次不打（批量指令可以重来）。

    ★ 2026-08-03 修：旧判据是 `want in (name, rmk) or want in (name.rstrip(DOG),
    rmk.rstrip(DOG))`——群名【或】备注任一匹配就放行。这是循环论证：**备注是我们自己
    打上去的，拿它确认"这是不是目标群"等于让错误替自己背书**。实测「肥肉测试1🐶」被
    错打到「泰国清迈旅居交流1群」头上后，再给「肥肉测试1」打备注时，模糊搜索命中那个
    错备注、切到清迈群，而 `rmk.rstrip(DOG)` 恰好等于目标群名 → 放行 → 又追加打一次
    →「肥肉测试1🐶肥肉测试1🐶」，**越重试错得越深**。
    现在：真实群名必须严格相等（备注只用于诊断错打、绝不用于放行），且目标群
    【不能已有别的备注】——SetGroupRemark 是追加，往有备注的群上打必然产生垃圾。"""
    info, err = _read_info(wx, group_name)
    if info is None:
        return False, err
    chat_type = str(info.get("chat_type") or "")
    if chat_type != "group":
        return False, f"当前窗口不是群聊（chat_type={chat_type or '未知'}）"
    name = str(info.get("chat_name") or "").strip()
    rmk = str(info.get("remark") or "").strip()
    want = group_name.strip()
    target = expect_remark or (want + DOG)

    # ① 读不到真实群名就不动手——没有它无从判断切到的是谁
    if not name:
        return False, "读不到当前窗口的真实群名"

    # ② 群名必须严格对上。备注在这里只用来把"错打"诊断出来，不用来放行
    if name != want:
        if rmk == target:
            return False, (f"备注「{target}」当前打在群「{name}」头上——这不是「{want}」，"
                           f"是一次错打，请先人工清掉该群备注再重打")
        return False, f"当前窗口是「{name}」，不是「{group_name}」"

    # ③ 群名对上了还得看备注：SetGroupRemark 是追加，往已有备注的群上打会变成「A🐶B🐶」
    if rmk and rmk != target:
        return False, f"该群已有备注「{rmk}」，再打会追加成「{rmk}{target}」"

    return True, ""


def verify_remark(wx, group_name: str, expect_remark: str) -> tuple[bool, str]:
    """打完备注后回读复核：备注真的变成 expect_remark 了吗，而且还在同一个群上。

    为什么不能只信 SetGroupRemark 的返回值：`wxresponse_ok` 连 None 都判成功
    （wxautox 有成功返回 None 的先例），等于几乎不设防。备注不可逆，登记表一旦记错
    就再也对不上微信了——宁可这次不 mark，让批量指令重来。

    ★ 2026-08-03 实测：`ChatInfo()` 只返回
    `{'chat_type','chat_name','group_member_count'}`，**没有 remark 字段**——
    只认 remark 的老判据在真机上会永远判失败。而备注生效的直接现象就是
    【窗口显示名变成备注】，所以显示名等于目标备注同样算数（切没切对群由打之前的
    confirm_group_window 保证，那时显示名还是真实群名）。"""
    info, err = _read_info(wx, group_name)
    if info is None:
        return False, err
    name = str(info.get("chat_name") or "").strip()
    rmk = str(info.get("remark") or "").strip()
    if expect_remark not in (name, rmk):
        return False, f"回读显示名「{name}」/备注「{rmk}」，都不是「{expect_remark}」"
    if name and name not in (group_name.strip(), expect_remark):
        return False, f"备注对了但窗口群名是「{name}」，不是「{group_name}」"
    return True, ""


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
        ok, why = confirm_group_window(wx, group_name, expect_remark=remark)
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

    if not wxresponse_ok(r):
        return False, f"SetGroupRemark 返回 {r!r}"

    # 打完回读复核：SetGroupRemark 的返回值几乎不设防（None 都算成功），而备注不可逆，
    # 登记表记错就再也对不上微信。复核不过就不 mark，留给批量指令重来。
    ok2, why2 = verify_remark(wx, group_name, remark)
    if not ok2:
        log("WARNING", f"打完备注复核不通过 {group_name}: {why2}")
        return False, f"打完复核不通过（{why2}）"

    registry.mark_remark_applied(group_name, remark)
    log("INFO", f"已打🐶备注：{group_name} -> {remark}")
    return True, remark
