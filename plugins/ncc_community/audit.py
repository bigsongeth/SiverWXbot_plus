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


# ---------------------------------------------------------------- 全量扫描并修备注

FIX_OK = "fix_ok"              # 备注已经是「真实群名🐶」，不用动
FIX_APPLY = "fix_apply"        # 没有备注，可以直接打上
FIX_CONFLICT = "fix_conflict"  # 已有别的备注 —— 只能人工清（SetGroupRemark 是追加）
FIX_SKIP = "fix_skip"          # 切不过去 / 读不到会话名 / 没群名的群，不动手
FIX_FAILED = "fix_failed"      # 打了但没成/复核不过
FIX_UNKNOWN = "fix_unknown"    # 有🐶备注但登记表里没这个名字（改名？错打？）

# ★ 微信群备注的长度上限，2026-08-03 实测出来的（血的教训，见下）：
# 给「AI+社区：我们到底需要什么样的社区」（+🐶 共 52 字节）打备注，SetGroupRemark
# 报成功、回读却还是原名；重跑一次后备注变成了「AI+社区：我们到底AI+社区：我们到底」
# ——超长的那次其实写进去了一截（被截断），第二次又追加了一截。
# 「游牧岛｜游牧护照持有者（会员群）」（同样 52 字节）一模一样的下场。
# 而已经成功打上的里面最长的「黄山NCC-黑多岛《老友记》总部👯🐶」正好 48 字节。
# 所以：算 UTF-8 字节、超过 48 的一律不碰，这类只能改短群名或者干脆不打🐶。
REMARK_MAX_BYTES = 48


def looks_spliced(name: str, min_len: int = 4) -> bool:
    """显示名里有一段【重复出现】= 多半是备注被追加过，不是真群名。

    SetGroupRemark 对已有备注是追加，所以重打同一个群会长成「名字名字」。
    带🐶的那种一眼能看出来，早年打的不带🐶的就只能靠这个：现场
    「【大理】春节串门一【大理】春节串」= 同一个群名的两段残片拼在一起，
    而登记表里那个群叫「【大理】春节串门一起玩！」。
    误报的代价只是跳过 + 报人工，所以宁可宽一点。"""
    n = (name or "").strip()
    for k in range(len(n) // 2, min_len - 1, -1):
        if n[:k] in n[k:]:
            return True
    return False


def plan_remark(display, known_names=()) -> tuple[str, str]:
    """给一个群定"该怎么办"。纯函数。

    ★ 2026-08-03 实测把设计前提改掉了：`ChatInfo()` 返回的只有
    `{'chat_type', 'chat_name', 'group_member_count'}` —— **根本没有 remark 字段**，
    而 `chat_name` 给的是【当前窗口显示名】：群一旦有备注，显示名就是备注本身。
    也就是说**微信侧根本读不到"真实群名"**，只读得到"现在显示成什么"。
    （旧版按"chat_name 是真名、remark 是备注"来判，会把 97 个已经打过🐶的群
    当成"没备注"，计划打成「NCC的朋友们16群🐶🐶」。）

    所以判定改成：显示名自己说明一切，真相源是 Notion 同步下来的群名集合。
      · 显示名以🐶结尾  → 已经打过备注；剥掉🐶在册就是达标，不在册要人看一眼
      · 显示名里夹着🐶  → 追加出来的垃圾备注（SetGroupRemark 对已有备注是追加），人工清
      · 显示名不含🐶    → 还没打备注，此时显示名就是真实群名，打它 + 🐶
    """
    name = (display or "").strip()
    if not name:
        return FIX_SKIP, "读不到会话名"
    known = set(known_names or ())

    if name.endswith(DOG):
        base = name[:-len(DOG)].strip()
        if DOG in base:
            return FIX_CONFLICT, f"备注是追加出来的垃圾「{name}」，只能人工清空重打"
        if not base:
            return FIX_SKIP, "备注只有一个🐶，读不出群名"
        if base in known:
            return FIX_OK, name
        return FIX_UNKNOWN, (f"备注「{name}」剥掉🐶后「{base}」不在登记表里——"
                             f"要么群在 Notion 改过名，要么这个备注打错了群")

    if DOG in name:
        return FIX_CONFLICT, f"备注是追加出来的垃圾「{name}」，只能人工清空重打"

    if looks_spliced(name):
        return FIX_CONFLICT, (f"「{name}」里有一段重复出现，多半是当年打的不带🐶的备注"
                              f"被追加过，先人工看一眼、清掉再打")

    # 没设过群名的群，微信显示成成员名拼接（「松爸、大曹、睿南…」），
    # 这种"名字"会随成员变化，拿它当群名打备注、写进 Notion 都没意义
    if "、" in name and name not in known:
        return FIX_SKIP, f"没有群名的群（微信显示成员名「{name}」），要不要打备注请人来定"

    want = name + DOG
    nbytes = len(want.encode("utf-8"))
    if nbytes > REMARK_MAX_BYTES:
        return FIX_SKIP, (f"「{want}」有 {nbytes} 字节，超过微信备注上限 "
                          f"{REMARK_MAX_BYTES} 字节，硬打会被截成垃圾——"
                          f"要么改短群名，要么这个群就不打🐶了")
    return FIX_APPLY, want


def extract_group_names(raw) -> list:
    """从 GetAllRecentGroups() 的返回里抽出会话显示名。

    文档只说 `List[Tuple]`，没说 tuple 里是什么，wxautox 又是编译发行的读不到源码，
    所以对 tuple/list/str/对象都兜一手，取第一个非空字符串当显示名。
    真实结构由管理群指令「扫群」实测确认（describe_raw）。
    """
    names, seen = [], set()
    for item in raw or []:
        name = ""
        if isinstance(item, str):
            name = item
        elif isinstance(item, (tuple, list)):
            name = next((str(x) for x in item if isinstance(x, str) and x.strip()), "")
        elif isinstance(item, dict):
            for k in ("name", "nickname", "chat_name", "who", "title"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    name = v
                    break
        else:
            for attr in ("name", "nickname", "chat_name", "who", "title"):
                v = getattr(item, attr, None)
                if isinstance(v, str) and v.strip():
                    name = v
                    break
            if not name:
                name = str(item)
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def describe_raw(raw, sample: int = 5) -> str:
    """把 GetAllRecentGroups() 的原始返回描述出来，用于实测确认结构。
    只读不改，安全。"""
    try:
        n = len(raw)
    except Exception:
        return f"返回不可迭代：type={type(raw).__name__} repr={raw!r}"[:800]
    lines = [f"GetAllRecentGroups 返回 {type(raw).__name__}，共 {n} 项。前 {min(sample, n)} 项："]
    for item in list(raw)[:sample]:
        t = type(item).__name__
        extra = ""
        if isinstance(item, (tuple, list)):
            extra = f" len={len(item)} 元素类型={[type(x).__name__ for x in item]}"
        lines.append(f"  · {t}{extra} → {item!r}"[:300])
    lines.append(f"抽出的显示名（前 {sample} 个）：{extract_group_names(raw)[:sample]}")
    return "\n".join(lines)


def summarize_fix(results, dry: bool = False) -> str:
    """把 [(群名, verdict, detail)] 汇总成发到管理群的报告。"""
    b = {}
    for name, verdict, detail in results:
        b.setdefault(verdict, []).append((name, detail))

    conflict = b.get(FIX_CONFLICT) or []
    skip = b.get(FIX_SKIP) or []
    failed = b.get(FIX_FAILED) or []
    todo = b.get(FIX_APPLY) or []
    unknown = b.get(FIX_UNKNOWN) or []

    head = "备注核对（预览，没动过微信）" if dry else "备注修复完成"
    lines = [f"{head}：微信里共 {len(results)} 个群。",
             f"  ✅ 备注已是「群名🐶」：{len(b.get(FIX_OK, []))}",
             f"  🔧 {'待打上' if dry else '这次打上'}：{len(todo)}"]
    if failed:
        lines.append(f"  ❌ 打失败：{len(failed)}")
    if unknown:
        lines.append(f"  🟡 备注在册对不上：{len(unknown)}")
    if conflict:
        lines.append(f"  ⚠️ 要人工清：{len(conflict)}")
    if skip:
        lines.append(f"  ◽ 跳过：{len(skip)}")

    if todo:
        lines.append(f"\n🔧 {'待打' if dry else '已打'}备注：")
        lines.extend(f"  - {n} → {d}" for n, d in todo)
    if conflict:
        lines.append("\n⚠️ 这些备注是追加出来的垃圾，我改不了——SetGroupRemark 对已有备注是"
                     "【追加】、空串也清不掉，硬打只会越接越长。"
                     "请在微信里手动清空这些群的备注，然后再发一次「修备注 全部」：")
        lines.extend(f"  - {n}：{d}" for n, d in conflict)
    if unknown:
        lines.append("\n🟡 这些群有🐶备注，但剥掉🐶后的名字不在登记表里"
                     "（多半是在 Notion 改过名，也可能是当年打错了群），要人看一眼：")
        lines.extend(f"  - {n}" for n, _ in unknown)
    if failed:
        lines.append("\n❌ 打备注失败（多为切窗口没切稳，可以重跑）：")
        lines.extend(f"  - {n}：{d}" for n, d in failed)
    if skip:
        lines.append("\n◽ 跳过的：")
        lines.extend(f"  - {n}：{d}" for n, d in skip)
    return "\n".join(lines)


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
