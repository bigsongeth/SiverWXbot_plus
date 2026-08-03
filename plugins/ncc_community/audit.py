# -*- coding: utf-8 -*-
"""备注名实核对 —— 查"A 群的🐶备注被打到了 B 群头上"。

为什么需要单独一个工具（而不是复用「检查群组」）：
「检查群组」只判 ChatWith 切没切过去。备注要是打错了群，切【是成功的】——只不过
切到的是那个被错打的群，于是检查报"可达 ✅"。错打对可达性检查是完全隐形的。

为什么必须【拿备注串去搜】而不是拿群名去搜：
错打时登记表往往还停在 remark_applied=False（打备注的动作切歪了、SetGroupRemark
落在别的群上，登记表这笔没记成）。这时按登记表寻址会用群名，切到的是真群、一切正常——
永远发现不了野备注。反过来用「群名🐶」去搜，微信会优先匹配备注，直接把那个被错打的
群捞出来，再读 ChatInfo 的真实群名一比对就现形：
    ChatWith("肥肉测试1🐶") → chat_name="泰国清迈旅居交流1群" → 名实不符 🔴

判定全部放在 classify() 里做成纯函数，UI/微信操作留在 forward.py，方便单测。
"""
from __future__ import annotations

DOG = "\U0001f436"  # 🐶

# 判定结果
OK = "ok"                      # 名实相符
MISAPPLIED = "misapplied"      # 🔴 备注打在了【另一个在册群】头上
RENAMED = "renamed"            # 🟡 切到的群不在登记表里，多半是群改了名
NOT_GROUP = "not_group"        # 🔴 备注落在了私聊头上
INCONCLUSIVE = "inconclusive"  # ⚪ 读不到真实群名，判不了
MISSING = "missing"            # 该备注在微信里搜不到


def classify(expect_name: str, info, known_names) -> tuple[str, str]:
    """判定「expect_name🐶」这个备注当前挂在谁头上。

    info 是 ChatInfo() 的返回（切到该备注后读的当前窗口），None 表示搜不到。
    known_names 是登记表里所有群名的集合——用它区分【错打】和【改名】：
    切到的群本身也在册 = 两个群的身份串了，几乎必定是错打；不在册则多半只是改了名。
    """
    want = (expect_name or "").strip()
    if not info:
        return MISSING, f"搜不到备注「{want}{DOG}」"

    chat_name = str(info.get("chat_name") or "").strip()
    remark = str(info.get("remark") or "").strip()
    chat_type = str(info.get("chat_type") or "")

    if chat_type and chat_type != "group":
        return NOT_GROUP, f"备注「{want}{DOG}」落在了非群聊上（chat_type={chat_type}，当前「{chat_name or remark}」）"

    # 备注字段读不到、chat_name 又只是备注串本身 → 拿不到真实群名，判不了
    if not chat_name or chat_name in (want + DOG, want):
        if not remark:
            return INCONCLUSIVE, f"读不到「{want}」的真实群名（chat_name={chat_name!r}）"
        return OK, ""

    if chat_name == want:
        return OK, ""

    if chat_name in known_names:
        return MISAPPLIED, f"备注「{want}{DOG}」实际打在了群「{chat_name}」头上"
    return RENAMED, f"备注「{want}{DOG}」所在的群现名「{chat_name}」，登记表里没有这个名字"


def summarize(results) -> str:
    """把 [(群名, verdict, detail)] 汇总成发到管理群的报告。"""
    buckets = {}
    for name, verdict, detail in results:
        buckets.setdefault(verdict, []).append((name, detail))

    total = len(results)
    ok_n = len(buckets.get(OK, []))
    lines = [f"备注核对完成：{total} 个群，名实相符 {ok_n} 个。"]

    for verdict, icon, title in (
        (MISAPPLIED, "🔴", "备注打错群了（A 的备注在 B 头上，两个群的身份串了）"),
        (NOT_GROUP, "🔴", "备注落在非群聊上"),
        (RENAMED, "🟡", "群可能改了名（去 Notion 更新群名即可）"),
        (INCONCLUSIVE, "⚪", "读不到真实群名，需人工看一眼"),
        (MISSING, "◽", "这个备注在微信里不存在（没打过，或已被覆盖）"),
    ):
        items = buckets.get(verdict) or []
        if not items:
            continue
        lines.append(f"\n{icon} {title}：{len(items)} 个")
        lines.extend(f"  - {d or n}" for n, d in items)

    if buckets.get(MISAPPLIED) or buckets.get(NOT_GROUP):
        lines.append("\n⚠️ 打错的备注只能人工在微信里清空后重打："
                     "wxautox 的 SetGroupRemark 对已有备注是【追加】、空串也清不掉。"
                     "清完把登记表的 remark_applied 复位再重打。")
    return "\n".join(lines)
