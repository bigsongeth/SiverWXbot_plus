# -*- coding: utf-8 -*-
"""回复里「必须有」的固定链接，由代码兜底补上，不指望模型自觉。纯函数，不 import 网关状态。

起因（2026-09-11，美食群）：hzfood 的 recommend_place 每次都返回「高德里打开：<短链>」，
技能里也写了要带上，可 20:03 那条「收好了：狮山路163号的遵义羊肉粉…」就是没带。
提示词里写"务必带上"管不住（与回复长度那次调研的结论一致），所以在一轮结束、回复交给机器人之前补：

  - 这轮收录成功了、回复里却没有那家店的「高德里打开」短链 → 从工具返回里取出来补上；
  - 这轮做了收录或推荐 → 结尾补一行固定的 footer（如「查看全部小众点评：<地图短链>」）。

按 URL 判重：模型自己写了就不再补。只追加到最后一条气泡，不新增气泡（群聊气泡数有上限）。
规则按技能名配置在网关 config 的 skill_links 里。
"""
from __future__ import annotations
import re
from typing import Iterable, List, Optional, Tuple

# 链接到空白、引号、反斜杠、中文标点为止：recommend_batch 的返回是 JSON，不截断会把 `",` 一起吞进链接
_URL_BODY = r"https?://[^\s\"'\\<>，。；）)」]+"
_URL = re.compile(_URL_BODY)
_LINK = re.compile(r"高德里打开[：:]\s*(" + _URL_BODY + ")")
_NAME = re.compile(r"「([^」]+)」")


def _short(name: str) -> str:
    """mcp__hzfood__recommend_place → recommend_place"""
    return str(name or "").rsplit("__", 1)[-1]


def _texts(content) -> List[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
    return out


def tool_activity(events: Optional[Iterable]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """(本轮调过的工具短名, [(工具短名, 返回全文)])。

    dsh 事件：tool/call 的 data 有 callId/name；tool/result 的 data.message.source.callId 对回去，
    正文在 data.message.content（[{type:text,text:…}]）。结构不对的事件一律跳过，不抛。
    """
    names, by_id, results = [], {}, []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if ev.get("type") == "tool/call":
            n = _short(data.get("name") or data.get("tool") or "")
            if n:
                names.append(n)
                by_id[str(data.get("callId"))] = n
        elif ev.get("type") == "tool/result":
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            src = msg.get("source") if isinstance(msg.get("source"), dict) else {}
            n = by_id.get(str(src.get("callId")), "")
            text = "\n".join(_texts(msg.get("content")))
            if text:
                results.append((n, text))
    return names, results


def ensure_links(bubbles: List[str], events: Optional[Iterable], skills: Iterable[str],
                 rules: dict) -> Tuple[List[str], List[str]]:
    """返回 (补过的气泡, 补了什么：["link"]/["footer"])。不改入参。"""
    if not bubbles or not rules:
        return list(bubbles or []), []
    rule = next((rules[s] for s in (skills or []) if s in rules), None)
    if not rule:
        return list(bubbles), []
    names, results = tool_activity(events)
    link_tools = set(rule.get("link_tools") or [])
    footer_tools = set(rule.get("footer_tools") or [])

    # 收录工具返回里的「高德里打开」短链（按出现顺序去重，带上店名以便多家时分清）
    links: List[Tuple[str, str]] = []
    for n, text in results:
        if n not in link_tools:
            continue
        prev = 0
        for m in _LINK.finditer(text):   # recommend_batch 一次返回好几家，逐个取
            # 店名取「上一个链接之后、这个链接之前」那段的第一个「…」——每家的结果都以「已收录「店名」」开头，
            # 取最近的会取到「文件夹「吃」」
            nm = _NAME.search(text, prev, m.start())
            prev = m.end()
            if all(m.group(1) != u for _, u in links):
                links.append((nm.group(1) if nm else "", m.group(1)))

    present = set(u for b in bubbles for u in _URL.findall(b))
    extra, added = [], []
    missing = [(nm, u) for nm, u in links if u not in present]
    # 一次收了很多家（批量）就不逐家贴链接了，太刷屏；结尾那行「查看全部」够用
    if len(links) > int(rule.get("max_links", 3)):
        missing = []
    if missing:
        for nm, u in missing:
            extra.append(f"「{nm}」高德里打开：{u}" if (len(links) > 1 and nm) else f"高德里打开：{u}")
        added.append("link")

    footer = rule.get("footer") or ""
    fu = _URL.findall(footer)
    if footer and footer_tools & set(names) and not (fu and fu[0] in present):
        extra.append(footer)
        added.append("footer")

    if not extra:
        return list(bubbles), []
    out = list(bubbles)
    out[-1] = out[-1].rstrip() + "\n" + "\n".join(extra)
    return out, added
