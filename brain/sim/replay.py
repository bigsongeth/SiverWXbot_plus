# -*- coding: utf-8 -*-
"""回放：把 Turn 序列逐条打到网关，出 Markdown 对照表 + 统计。

用法（网关已在 8500 跑着，且 FEIROU_DATA 指向一个专门的 sim 数据目录，别污染正式记忆）：
  python3 brain/sim/replay.py --qa ~/sim/qa-20260904.jsonl:8 \
      --memory "memory/FeiRou_NCC/松爸/松爸_memory.json:松爸:private:15" \
      --memory "memory/FeiRou_NCC/肥肉测试1🐶/肥肉测试1🐶_memory.json:肥肉测试1🐶:group" \
      --replied-only --limit 100 --out ~/sim/replay-round1.md

规格：
  --qa      path[:N]                      取最后 N 条
  --memory  path:会话名:group|private[:N]  取最后 N 条 friend 轮次（最近的最有代表性）
  --replied-only  memory 来源只保留 old_reply 非空的轮次（老机器人没答的多半是没被点名，对照没意义）
每条回放是一次真 dsh 调用（30–115 秒），条数自己掂量。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from brain.gateway import shape  # noqa: E402
from brain.gateway.config import DEFAULTS  # noqa: E402
from brain.sim import sources  # noqa: E402

_CLOSING = re.compile(r"(需要我|要不要|还要|想知道|继续吗|要我|想听)[^。\n]*[?？]\s*$")
_FIELDS = ("conversation", "is_group", "sender", "text", "prime")


def post(url: str, payload: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/reply", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def parse_memory_spec(spec: str) -> Tuple[str, str, bool, Optional[int]]:
    """path:会话名:group|private[:N] → (path, conv, is_group, limit)。路径本身可能含冒号，从右边切。"""
    parts = spec.rsplit(":", 3)
    if len(parts) == 4 and parts[3].isdigit():
        path, conv, kind, n = parts
        return path, conv, kind == "group", int(n)
    path, conv, kind = spec.rsplit(":", 2)
    return path, conv, kind == "group", None


def parse_qa_spec(spec: str) -> Tuple[str, Optional[int]]:
    """path[:N] → (path, limit)。"""
    head, sep, tail = spec.rpartition(":")
    if sep and tail.isdigit():
        return head, int(tail)
    return spec, None


def select(turns: List[dict], limit: Optional[int], replied_only: bool) -> List[dict]:
    """先按 replied_only 过滤，再取最后 limit 条。"""
    if replied_only:
        turns = [t for t in turns if (t.get("old_reply") or "").strip()]
    if limit is not None:
        turns = turns[-limit:] if limit > 0 else []
    return list(turns)


def run(turns: List[dict], gateway_url: str, out_md: str) -> dict:
    rows = []
    stats = {"n": 0, "over_budget": 0, "repeat_opener": 0, "closing": 0, "no_reply": 0, "ms": []}
    openers = []
    for i, t in enumerate(turns, 1):
        t0 = time.time()
        try:
            res = post(gateway_url, {k: t.get(k) for k in _FIELDS})
        except Exception as e:  # 网关挂了/超时也要把这一行记下来，别让整轮回放白跑
            res = {"error": "%s: %s" % (type(e).__name__, e)}
        ms = int((time.time() - t0) * 1000)
        stats["n"] += 1
        stats["ms"].append(ms)
        bubbles = [b for b in (res.get("bubbles") or []) if isinstance(b, str)]
        new = "\n".join(bubbles) if bubbles else ("〔不接话：%s〕" % res.get("reason", res.get("error", "?")))
        budget = shape.budget(t["text"], bool(t["is_group"]), DEFAULTS)
        n = sum(shape.text_len(b) for b in bubbles)
        flags = []
        if not bubbles:
            stats["no_reply"] += 1
        if n > budget:
            stats["over_budget"] += 1
            flags.append("超预算")
        for b in bubbles:
            o = re.sub(r"\s+", "", b)[:8]
            if o in openers:
                stats["repeat_opener"] += 1
                flags.append("重复开头")
                break
            openers.append(o)
        if bubbles and _CLOSING.search(bubbles[-1]):
            stats["closing"] += 1
            flags.append("收尾套话")
        rows.append("| %d | %s | %s | %s | %s | 预算%d/实%d/%d泡 %s | %d |" % (
            i, _cell(t["conversation"]), _cell("%s: %s" % (t["sender"], t["text"])), _cell(t.get("old_reply", "")),
            _cell(new), budget, n, len(bubbles), " ".join(flags), ms))
        print("#%d/%d %dms %s %r" % (i, len(turns), ms, flags, new[:40]), flush=True)
    avg = int(sum(stats["ms"]) / len(stats["ms"])) if stats["ms"] else 0
    os.makedirs(os.path.dirname(os.path.abspath(out_md)) or ".", exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# 回放对照表 %s\n\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("共 %d 条：超预算 %d，重复开头 %d，收尾套话 %d，不接话 %d，平均 %d ms\n\n" % (
            stats["n"], stats["over_budget"], stats["repeat_opener"], stats["closing"], stats["no_reply"], avg))
        f.write("| # | 会话 | 发言 | 原回复 | 新回复 | 闸门 | 耗时ms |\n|---|---|---|---|---|---|---|\n")
        f.write("\n".join(rows) + "\n")
    stats["avg_ms"] = avg
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qa", action="append", default=[], help="qa jsonl 文件 path[:N]，可多次")
    ap.add_argument("--memory", action="append", default=[], help="path:会话名:group|private[:N]，可多次")
    ap.add_argument("--replied-only", action="store_true", help="memory 来源只保留有老回复的轮次")
    ap.add_argument("--gateway", default="http://127.0.0.1:8500")
    ap.add_argument("--limit", type=int, default=100, help="总条数上限")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    turns = []
    for spec in a.qa:
        path, n = parse_qa_spec(spec)
        turns += select(sources.from_qa_jsonl(os.path.expanduser(path)), n, False)
    for spec in a.memory:
        path, conv, is_group, n = parse_memory_spec(spec)
        turns += select(sources.from_memory_json(os.path.expanduser(path), conv, is_group), n, a.replied_only)
    turns = turns[:a.limit]
    print("共 %d 条待回放 → %s" % (len(turns), a.gateway), flush=True)
    st = run(turns, a.gateway, os.path.expanduser(a.out))
    print(json.dumps({k: v for k, v in st.items() if k != "ms"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
