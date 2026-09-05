# 肥肉大脑 期 0 + 期 1 实施计划（大脑本体 + 回放模拟）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出一个能在 mac 本机跑起来的「肥肉大脑」：常驻 dsh + 网关 + 说话工具 + 回复闸门 + 人设/技能/记忆工作区，并用真实聊天记录回放出一份「原回复 / 新回复」对照表。

**Architecture:** 网关（纯标准库 Python）常驻并通过 stdio JSON-RPC 驱动一个 `dsh --profile sdk` 子进程；模型只能通过本地 MCP 工具 `wx_reply` / `no_reply` 说话，工具是个哑桥，把调用回传给网关做校验（气泡数、长度预算、反口头禅）；一切记忆写在沙盒工作区里；NCC 知识库通过网关代理的只读检索端点访问。

**Tech Stack:** Python 3.9+（标准库，无第三方依赖）、Node 24 + `@deepseek-ai/dsh@0.1.2-rc.1`、MCP stdio JSON-RPC、songkey（`key.bigsong.site`）。

**Spec:** `docs/superpowers/specs/2026-09-05-dsh-brain-design.md`

## Global Constraints

- 模型主用 `songkey/songkey-auto`；期 0 若 MCP 工具调用成功率 < 9/10，改 `deepseek-v4-flash`。
- 大脑只能通过 `wx_reply` 说话；正文里的文字一律不发。群 ≤ 2 条气泡，私聊 ≤ 3 条。
- 长度预算 `budget = clamp(30 + 2.5 × len(对方消息), 40, 群 150 / 私聊 220)`，字数按去空白字符数。
- 工作区里只有肥肉自己的东西；dsh 进程 cwd = 工作区；沙箱 `workspace-write`；审批策略 `never`；不挂 bash / pwsh / jobs / subagent / tool-web。
- 签到往来（触发词、兑换码、key.bigsong.site）不喂给大脑。
- 共享知识 `knowledge/shared.md` 大脑只读；写入只经「提议 → 通过」。个人/群记忆单文件 ≤ 4096 字节。
- 所有 Python 在 mac 上裸跑：`PYTHONPATH=. python3 tests/test_brain_xxx.py`（**别用 `-m unittest tests.xxx`**，会被 anaconda 的 tests 包遮蔽）。
- Python 代码兼容 3.9（mac-mini 系统 python 是 3.9.6）：用 `from __future__ import annotations` + `typing.Optional`，不用 `X | None`、不用 `match`。
- 每个任务结束提交一次；commit 只加本任务的文件，仓库里 `logger.py`、`plugins/gh_trending_note/last_sent.txt`、`tests/test_stdout_encoding.py` 是别的会话的未提交改动，**别碰**。
- dsh 相关路径（mac 本机与 mac-mini 相同）：`NODE=~/.nvm/versions/node/v24.19.0/bin/node`，`DSH_BIN=~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js`。
- 运行数据目录 `FEIROU_DATA`（默认 `~/feirou-brain-data`）不进库；`brain/` 下只放代码、人设、技能、共享知识种子。
- **复用，别重写**（2026-09-05 二次调研定的）：剥 Markdown 用 `plugins.reply_shape.strip_markdown`；
  历史过滤先过 `plugins.context_guard.guard.filter_history`；签到触发词用 `plugins.wechat_checkin.handler.TRIGGERS`；
  美食地图不写检索工具，直接挂公网 MCP `https://food.bigsong.site/mcp`；技能 `hz-food-map` 的真相源是本机
  `~/Personal/hz-food-map/deploy/dsh/SKILL.md`。这些模块都不 import wxbot_core，mac 上裸跑没问题。
- 网关除 `/reply` 外还要有 OpenAI 兼容的 `/v1/chat/completions`（模型名编码会话：`feirou:group:<群名>` / `feirou:chat:<昵称>`），
  杭州美食群靠它零改动切过来。

---

## 文件结构

```
brain/
  __init__.py
  run.py                     入口：准备数据目录 → 渲染 patch → 起网关
  gateway/
    __init__.py
    config.py                默认值 + 读 <data>/config.json（缺失即默认）
    shape.py                 纯函数：字数/预算/剥收尾套话/反口头禅（剥 Markdown 复用 reply_shape）
    compat.py                OpenAI 兼容路由的解析：模型名→会话、最后一条 user→发言人+正文、messages→prime
    validate.py              validate_reply()：把 shape 的规则组合成"接受/退回"决定
    replies.py               RecentReplies：最近已发气泡（全局 50 / 会话 20），落盘
    dsh_client.py            DshClient：起 dsh sdk 子进程、JSON-RPC、prompt 到 idle
    profile.py               渲染 cordis.patch.yml、准备 DSH_HOME
    context.py               用户消息前缀行、技能匹配、prime 过滤
    proposals.py             共享知识提议 CRUD + approve 追加 shared.md
    memory_guard.py          记忆文件超限截尾
    kb.py                    调 mac-mini /retrieve 的只读客户端
    server.py                Gateway 类 + HTTP 路由
  mcp/
    feirou_tools.py          stdio MCP 哑桥：4 个工具全转发到网关 /tool/<name>
    grok_search/server.js    从 ~/.claude/mcp-servers/grok-search/server.js 原样拷来
  profile/
    cordis.patch.yml.tmpl    sdk profile 的 patch 模板
    settings.yaml.tmpl       DSH_HOME/settings.yaml 模板
  workspace/                 种子（首次启动复制进 <data>/workspace，已存在不覆盖）
    PERSONA.md
    knowledge/shared.md
    skills/index.json
    skills/hz-food-map/SKILL.md
    skills/ncc-community/SKILL.md
  verify/
    p0_resume.py             期 0：重启后同 sessionId 是否接续
    p0_tools.py              期 0：songkey-auto 调 MCP 工具成功率
    echo_mcp.py              期 0 用的最小 MCP 服务
  sim/
    sources.py               读 qa jsonl / 机器人 memory json 成统一的对话序列
    replay.py                回放 → Markdown 对照表
    judge.py                 可选：songkey 模型四轴打分
  README.md
tests/
  test_brain_shape.py  test_brain_validate.py  test_brain_replies.py
  test_brain_dsh_client.py (+ tests/fake_dsh.py)  test_brain_context.py
  test_brain_proposals.py  test_brain_server.py  test_brain_mcp.py  test_brain_sim.py  test_brain_compat.py
```

---

### Task 1: 包骨架 + 配置 + 回复形状纯函数

**Files:**
- Create: `brain/__init__.py`, `brain/gateway/__init__.py`, `brain/gateway/config.py`, `brain/gateway/shape.py`
- Test: `tests/test_brain_shape.py`

**Interfaces:**
- Produces: `config.DEFAULTS: dict`, `config.load(data_dir) -> dict`
- Produces: `shape.text_len(s) -> int`, `shape.budget(incoming, is_group, cfg) -> int`, `shape.max_bubbles(is_group, cfg) -> int`, `shape.strip_markdown(s) -> str`, `shape.strip_closing(bubbles) -> list[str]`, `shape.repeats(bubble, recent, threshold=0.5) -> Optional[str]`, `shape.truncate_to(bubbles, budget) -> list[str]`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_shape.py
# -*- coding: utf-8 -*-
"""回复形状纯函数：预算 / 剥 Markdown / 剥收尾套话 / 反口头禅 / 截断。"""
from __future__ import annotations
import unittest
from brain.gateway import shape
from brain.gateway.config import DEFAULTS


class BudgetTest(unittest.TestCase):
    def test_short_incoming_hits_floor(self):
        self.assertEqual(shape.budget("滴滴", True, DEFAULTS), 40)

    def test_scales_with_incoming(self):
        # 30 + 2.5*20 = 80
        self.assertEqual(shape.budget("一" * 20, True, DEFAULTS), 80)

    def test_group_and_private_caps(self):
        long = "字" * 200
        self.assertEqual(shape.budget(long, True, DEFAULTS), 150)
        self.assertEqual(shape.budget(long, False, DEFAULTS), 220)

    def test_text_len_ignores_whitespace(self):
        self.assertEqual(shape.text_len(" a b\n c "), 3)

    def test_max_bubbles(self):
        self.assertEqual(shape.max_bubbles(True, DEFAULTS), 2)
        self.assertEqual(shape.max_bubbles(False, DEFAULTS), 3)


class StripTest(unittest.TestCase):
    def test_strip_markdown_reuses_reply_shape(self):
        out = shape.strip_markdown("**加粗** 和\n# 标题\n---\n正文")
        self.assertNotIn("**", out); self.assertNotIn("#", out); self.assertNotIn("---", out)
        self.assertIn("加粗", out); self.assertIn("正文", out)

    def test_strip_closing_drops_last_bubble(self):
        out = shape.strip_closing(["大理还在运营。", "需要我把地址发你吗？"])
        self.assertEqual(out, ["大理还在运营。"])

    def test_strip_closing_keeps_real_question(self):
        out = shape.strip_closing(["你说的是黄山还是大理？"])
        self.assertEqual(out, ["你说的是黄山还是大理？"])

    def test_strip_closing_single_bubble_drops_last_sentence(self):
        out = shape.strip_closing(["大理还在运营，黑多岛也在。还要我继续介绍吗？"])
        self.assertEqual(out, ["大理还在运营，黑多岛也在。"])

    def test_strip_closing_leaves_non_question_alone(self):
        out = shape.strip_closing(["需要我的话随时叫。"])
        self.assertEqual(out, ["需要我的话随时叫。"])


class RepeatTest(unittest.TestCase):
    def test_same_opener_is_repeat(self):
        r = shape.repeats("我刚从键盘上趴起来，看到你说滴滴", ["我刚从键盘上趴起来，刷到消息"])
        self.assertEqual(r, "我刚从键盘上趴起来，刷到消息")

    def test_high_ngram_overlap_is_repeat(self):
        r = shape.repeats("大理据点还在运营中哦", ["嗯，大理据点还在运营中哦"])
        self.assertIsNotNone(r)

    def test_different_reply_not_repeat(self):
        self.assertIsNone(shape.repeats("黄山这周有活动", ["大理据点还在运营"]))


class TruncateTest(unittest.TestCase):
    def test_truncate_drops_overflow_bubbles(self):
        out = shape.truncate_to(["一" * 30, "二" * 30], 40)
        self.assertEqual(out, ["一" * 30])

    def test_truncate_cuts_single_bubble_at_sentence(self):
        out = shape.truncate_to(["第一句。第二句很长很长很长。第三句。"], 8)
        self.assertEqual(out, ["第一句。"])

    def test_truncate_hard_cut_when_no_sentence_end(self):
        out = shape.truncate_to(["一" * 50], 10)
        self.assertEqual(out, ["一" * 10])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_shape.py`
Expected: `ModuleNotFoundError: No module named 'brain'`

- [ ] **Step 3: 写实现**

```python
# brain/__init__.py
# 肥肉大脑：常驻 dsh 智能体 + 网关 + 回复闸门。设计见 docs/superpowers/specs/2026-09-05-dsh-brain-design.md
```

```python
# brain/gateway/__init__.py
```

```python
# brain/gateway/config.py
# -*- coding: utf-8 -*-
"""网关配置：默认值写死在这里，<data>/config.json 里的同名键覆盖。"""
from __future__ import annotations
import json
import os

DEFAULTS = {
    "port": 8500,
    "bind": "0.0.0.0",
    "provider": "songkey",
    "model": "songkey-auto",
    "turn_timeout_sec": 60,
    "lock_timeout_sec": 70,
    "budget": {"base": 30, "factor": 2.5, "min": 40, "max_group": 150, "max_private": 220},
    "max_bubbles_group": 2,
    "max_bubbles_private": 3,
    "recent_global": 50,
    "recent_per_conversation": 20,
    "repeat_threshold": 0.5,
    "memory_max_bytes": 4096,
    "prime_count": 20,
    "kb_url": "http://100.71.182.5:8434",
    "kb_timeout_sec": 20,
}


def load(data_dir: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(data_dir, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
```

```python
# brain/gateway/shape.py
# -*- coding: utf-8 -*-
"""回复形状的纯函数。不 import 任何网关状态，mac 上裸跑单测。

规则来自设计文档 §4.4：长度预算、剥 Markdown、剥收尾套话、反口头禅、截断。
"""
from __future__ import annotations
import re
from typing import Iterable, List, Optional

_WS = re.compile(r"\s+")
_SENT_END = "。！!？?~～"
_CLOSING = re.compile(r"(需要我|要不要|还要|想知道|继续吗|要我|想听)")


def text_len(s: Optional[str]) -> int:
    return len(_WS.sub("", s or ""))


def budget(incoming: str, is_group: bool, cfg: dict) -> int:
    b = cfg["budget"]
    cap = b["max_group"] if is_group else b["max_private"]
    raw = b["base"] + b["factor"] * text_len(incoming)
    return int(max(b["min"], min(cap, raw)))


def max_bubbles(is_group: bool, cfg: dict) -> int:
    return int(cfg["max_bubbles_group"] if is_group else cfg["max_bubbles_private"])


# 剥 Markdown 复用机器人插件那份（只剥 **加粗** / # 标题 / --- 分隔线，保留列表符号与代码块），别再写一份。
from plugins.reply_shape import strip_markdown  # noqa: E402,F401


def _split_sentences(s: str) -> List[str]:
    parts, buf = [], ""
    for ch in s:
        buf += ch
        if ch in _SENT_END:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    return [p for p in parts if p.strip()]


def strip_closing(bubbles: List[str]) -> List[str]:
    """最后一条以问号结尾且含套话 → 整条剥掉；它是唯一一条时只剥最后一句；
    整条就是一句问句则留着（可能是必要的反问）。"""
    if not bubbles:
        return bubbles
    last = bubbles[-1].strip()
    if not last.endswith(("?", "？")) or not _CLOSING.search(last):
        return bubbles
    if len(bubbles) > 1:
        return bubbles[:-1]
    sents = _split_sentences(last)
    if len(sents) <= 1:
        return bubbles
    return ["".join(sents[:-1]).strip()]


def _opener(s: str, n: int = 8) -> str:
    return _WS.sub("", s or "")[:n]


def _ngrams(s: str, n: int = 4) -> set:
    t = _WS.sub("", s or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def repeats(bubble: str, recent: Iterable[str], threshold: float = 0.5) -> Optional[str]:
    """新气泡和最近某条开头 8 字相同，或 4-gram Jaccard 超阈值，返回撞上的那条；否则 None。"""
    o = _opener(bubble)
    g = _ngrams(bubble)
    for r in recent:
        if o and _opener(r) == o:
            return r
        gr = _ngrams(r)
        if g and gr:
            j = len(g & gr) / len(g | gr)
            if j > threshold:
                return r
    return None


def truncate_to(bubbles: List[str], limit: int) -> List[str]:
    """按预算截断：整条放得下就留，放不下的那条按句子截，一句都放不下就硬切。"""
    out, used = [], 0
    for b in bubbles:
        n = text_len(b)
        if used + n <= limit:
            out.append(b)
            used += n
            continue
        room = limit - used
        if room <= 0:
            break
        kept = ""
        for s in _split_sentences(b):
            if text_len(kept + s) <= room:
                kept += s
            else:
                break
        if not kept:
            compact = _WS.sub("", b)
            kept = compact[:room]
        out.append(kept.strip())
        break
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_shape.py`
Expected: `OK`（15 个用例）

- [ ] **Step 5: 提交**

```bash
git add brain/__init__.py brain/gateway/__init__.py brain/gateway/config.py brain/gateway/shape.py tests/test_brain_shape.py
git commit -m "feat(brain): 骨架 + 配置 + 回复形状纯函数（预算/剥Markdown/剥收尾/反口头禅/截断）"
```

---

### Task 2: DshClient（常驻 dsh sdk 子进程的 JSON-RPC 客户端）

**Files:**
- Create: `brain/gateway/dsh_client.py`, `tests/fake_dsh.py`
- Test: `tests/test_brain_dsh_client.py`

**Interfaces:**
- Produces: `class DshClient(argv: list[str], cwd: str, env: dict)`，方法 `start()`, `initialize(cwd, provider, model) -> dict`, `prompt(session_id, text, timeout_sec) -> TurnResult`, `alive() -> bool`, `stop()`
- Produces: `TurnResult(reasoning: str, text: str, events: list[dict], timed_out: bool)`
- 协议事实（2026-09-05 实测）：`initialize` 参数 `{cwd, provider, model}`；`session/prompt` 参数 `{sessionId, contentBlocks:[{type:"text",text}]}` 返回 `{messageId}`；通知 `session.event {sessionId, event{type,...}}`（type 有 `turn/start`、`assistant/message`、`turn/end` 等，`assistant/message` 的 `data.message.content` 是 `[{type:"reasoning",text}|{type:"text",text}]`）和 `session.status {sessionId, status:"running"|"idle"}`。

- [ ] **Step 1: 写假 dsh（测试替身，也给后面的 server 测试用）**

```python
# tests/fake_dsh.py
# -*- coding: utf-8 -*-
"""假 dsh sdk 服务：stdin 读 JSON-RPC 行，模拟 initialize / session/prompt / shutdown。
prompt 文本里含 "SLOW" 时 sleep 3 秒再回；含 "CRASH" 时直接退出进程。"""
import json
import sys
import time

seq = 0


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def event(session, typ, data):
    global seq
    seq += 1
    out({"jsonrpc": "2.0", "method": "session.event",
         "params": {"sessionId": session, "event": {"type": typ, "seq": seq, "data": data}}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        out({"jsonrpc": "2.0", "id": i, "result": {"serverInfo": {"name": "fake-dsh", "version": "0"}}})
    elif m == "session/prompt":
        s = p["sessionId"]
        text = p["contentBlocks"][0]["text"]
        out({"jsonrpc": "2.0", "id": i, "result": {"messageId": "m1"}})
        if "CRASH" in text:
            sys.exit(3)
        out({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": s, "status": "running"}})
        event(s, "turn/start", {"turn": 1})
        if "SLOW" in text:
            time.sleep(3)
        event(s, "assistant/message", {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
            {"type": "reasoning", "text": "thinking about: " + text},
            {"type": "text", "text": "echo: " + text}]}})
        event(s, "turn/end", {"turn": 1})
        out({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": s, "status": "idle"}})
    elif m == "shutdown":
        out({"jsonrpc": "2.0", "id": i, "result": {}})
        sys.exit(0)
```

- [ ] **Step 2: 写失败的测试**

```python
# tests/test_brain_dsh_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import unittest
from brain.gateway.dsh_client import DshClient

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_dsh.py")]


class DshClientTest(unittest.TestCase):
    def setUp(self):
        self.c = DshClient(FAKE, cwd=os.getcwd(), env=dict(os.environ))
        self.c.start()
        self.c.initialize(os.getcwd(), "songkey", "songkey-auto")

    def tearDown(self):
        self.c.stop()

    def test_prompt_returns_text_and_reasoning(self):
        r = self.c.prompt("s1", "你好", timeout_sec=5)
        self.assertEqual(r.text, "echo: 你好")
        self.assertIn("thinking about", r.reasoning)
        self.assertFalse(r.timed_out)
        self.assertIn("turn/end", [e["type"] for e in r.events])

    def test_sessions_do_not_mix(self):
        a = self.c.prompt("a", "A", timeout_sec=5)
        b = self.c.prompt("b", "B", timeout_sec=5)
        self.assertEqual(a.text, "echo: A")
        self.assertEqual(b.text, "echo: B")

    def test_timeout_flag(self):
        r = self.c.prompt("s1", "SLOW", timeout_sec=1)
        self.assertTrue(r.timed_out)

    def test_alive_false_after_crash(self):
        self.c.prompt("s1", "CRASH", timeout_sec=2)
        self.assertFalse(self.c.alive())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_dsh_client.py`
Expected: `ModuleNotFoundError: No module named 'brain.gateway.dsh_client'`

- [ ] **Step 4: 写实现**

```python
# brain/gateway/dsh_client.py
# -*- coding: utf-8 -*-
"""常驻 `dsh --profile sdk` 子进程的 JSON-RPC 客户端。

线路事实（2026-09-05 在 mac 上对 dsh 0.1.2-rc.1 实测）：
- stdout 每行一个 JSON-RPC 2.0 帧；有 id+method 是请求，只有 id 是响应，只有 method 是通知。
- initialize {cwd, provider, model} 必须先成功，session/prompt 才被接受。
- 一轮的结束判据：收到该 session 的 turn/end 事件之后的 session.status=idle。
"""
from __future__ import annotations
import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TurnResult:
    reasoning: str = ""
    text: str = ""
    events: List[dict] = field(default_factory=list)
    timed_out: bool = False


class DshClient:
    def __init__(self, argv: List[str], cwd: str, env: dict, stderr_path: Optional[str] = None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.stderr_path = stderr_path
        self.p: Optional[subprocess.Popen] = None
        self._id = 0
        self._pending: Dict[int, queue.Queue] = {}
        self._notes: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    # ---- 进程 ----
    def start(self) -> None:
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def stop(self) -> None:
        if not self.alive():
            return
        try:
            self._request("shutdown", {}, timeout=3)
        except Exception:
            pass
        try:
            self.p.terminate()
            self.p.wait(timeout=3)
        except Exception:
            self.p.kill()

    # ---- 线路 ----
    def _reader(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "id" in m and "method" not in m:
                q = self._pending.get(m["id"])
                if q:
                    q.put(m)
            elif "method" in m:
                sid = (m.get("params") or {}).get("sessionId")
                if sid:
                    self._session_queue(sid).put(m)

    def _session_queue(self, sid: str) -> queue.Queue:
        with self._lock:
            if sid not in self._notes:
                self._notes[sid] = queue.Queue()
            return self._notes[sid]

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            self._pending[rid] = queue.Queue()
        frame = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, ensure_ascii=False)
        self.p.stdin.write(frame + "\n")
        self.p.stdin.flush()
        try:
            m = self._pending[rid].get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(method)
        finally:
            self._pending.pop(rid, None)
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result") or {}

    # ---- 会话 ----
    def initialize(self, cwd: str, provider: str, model: str) -> dict:
        return self._request("initialize", {"cwd": cwd, "provider": provider, "model": model}, timeout=60)

    def prompt(self, session_id: str, text: str, timeout_sec: float) -> TurnResult:
        q = self._session_queue(session_id)
        while not q.empty():  # 丢掉上一轮残留的通知
            q.get_nowait()
        res = TurnResult()
        try:
            self._request("session/prompt", {"sessionId": session_id,
                                             "contentBlocks": [{"type": "text", "text": text}]}, timeout=10)
        except Exception as e:
            res.timed_out = True
            res.reasoning = f"prompt failed: {e}"
            return res
        deadline = time.time() + timeout_sec
        turn_ended = False
        while time.time() < deadline:
            if not self.alive():
                res.timed_out = True
                return res
            try:
                m = q.get(timeout=0.5)
            except queue.Empty:
                continue
            meth, prm = m.get("method"), m.get("params") or {}
            if meth == "session.event":
                ev = prm.get("event") or {}
                res.events.append(ev)
                typ = ev.get("type")
                if typ == "assistant/message":
                    for b in (((ev.get("data") or {}).get("message") or {}).get("content") or []):
                        if b.get("type") == "reasoning":
                            res.reasoning += b.get("text", "")
                        elif b.get("type") == "text":
                            res.text += b.get("text", "")
                elif typ == "turn/end":
                    turn_ended = True
            elif meth == "session.status" and prm.get("status") == "idle" and turn_ended:
                return res
        res.timed_out = True
        return res
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_dsh_client.py`
Expected: `OK`（4 个用例，约 5 秒）

- [ ] **Step 6: 提交**

```bash
git add brain/gateway/dsh_client.py tests/fake_dsh.py tests/test_brain_dsh_client.py
git commit -m "feat(brain): DshClient 常驻 dsh sdk 子进程的 JSON-RPC 客户端 + 假 dsh 测试替身"
```

---

### Task 3: profile 模板 + DSH_HOME 准备 + 期 0 验证

**Files:**
- Create: `brain/profile/cordis.patch.yml.tmpl`, `brain/profile/settings.yaml.tmpl`, `brain/gateway/profile.py`, `brain/verify/echo_mcp.py`, `brain/verify/p0_resume.py`, `brain/verify/p0_tools.py`
- Test: `tests/test_brain_profile.py`

**Interfaces:**
- Consumes: `DshClient`（Task 2）
- Produces: `profile.render_patch(persona: str, feirou_mcp_argv: list[str], grok_server_js: Optional[str], songkey_key: str, extra_yaml: str = "") -> str`（返回 patch 文本）
- Produces: `profile.prepare_dsh_home(dsh_home: str, songkey_key: str, model: str) -> None`（写 settings.yaml，幂等）
- Produces: `profile.dsh_argv(node: str, dsh_bin: str, patch_path: str) -> list[str]`

- [ ] **Step 1: 写模板**

```yaml
# brain/profile/cordis.patch.yml.tmpl
# 肥肉大脑的 sdk profile 覆盖层。由 gateway/profile.py 渲染，占位符：__PERSONA__ __FEIROU_CMD__ __FEIROU_ARGS__ __GROK_BLOCK__
# 关掉的东西：shell 类工具、子代理、dsh 自带联网。审批 never（沙箱兜底，不存在人来点允许）。
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-jobs
  disabled: true
- id: bash-sandbox
  disabled: true
- id: pwsh-sandbox
  disabled: true
- id: tool-subagent
  disabled: true
- id: tool-subagent-fork
  disabled: true
- id: tool-subagent-control
  disabled: true
- id: tool-subagent-list-agents
  disabled: true
- id: tool-web
  disabled: true
- id: web-fetch-http
  disabled: true
- id: approval
  config:
    policy: never
- id: sandbox-policy
  config:
    mode: workspace-write
- id: system-prompt
  config:
    persona: |
__PERSONA__
- insert:
    - id: mcp-feirou
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: feirou
        transport: stdio
        command: __FEIROU_CMD__
        args:
__FEIROU_ARGS__
        toolCallTimeoutMs: 60000
        failOnStartupError: true
    - id: mcp-hzfood
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: hzfood
        transport: streamable-http
        url: https://food.bigsong.site/mcp
        toolCallTimeoutMs: 60000
        failOnStartupError: false
__GROK_BLOCK__
```

```yaml
# brain/profile/settings.yaml.tmpl
llm-pi-ai:
  providers:
    songkey:
      apiKeyEnv: SONGKEY_API_KEY
      api: openai-completions
      baseURL: https://key.bigsong.site/v1
      models:
        - id: songkey-auto
        - id: deepseek-v4-flash
        - id: glm-5.2
agent-default-model:
  provider: songkey
  model: __MODEL__
```

- [ ] **Step 2: 写失败的测试**

```python
# tests/test_brain_profile.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import tempfile
import unittest
from brain.gateway import profile


class ProfileTest(unittest.TestCase):
    def test_render_patch_indents_persona_and_args(self):
        txt = profile.render_patch("你是肥肉。\n第二行。", ["/usr/bin/python3", "/x/feirou_tools.py"], None, "sk-test")
        self.assertIn("    persona: |\n      你是肥肉。\n      第二行。\n", txt)
        self.assertIn("        command: /usr/bin/python3\n        args:\n          - /x/feirou_tools.py\n", txt)
        self.assertNotIn("__GROK_BLOCK__", txt)
        self.assertNotIn("sk-test", txt)  # 没给 grok 就不该出现 key

    def test_render_patch_with_grok(self):
        txt = profile.render_patch("p", ["py", "t.py"], "/srv/server.js", "sk-test")
        self.assertIn("serverName: grok", txt)
        self.assertIn("SONGKEY_API_KEY: sk-test", txt)

    def test_prepare_dsh_home_writes_settings_once(self):
        d = tempfile.mkdtemp()
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        s = open(os.path.join(d, "settings.yaml"), encoding="utf-8").read()
        self.assertIn("model: songkey-auto", s)
        with open(os.path.join(d, "settings.yaml"), "a", encoding="utf-8") as f:
            f.write("# 人手改过\n")
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        self.assertIn("人手改过", open(os.path.join(d, "settings.yaml"), encoding="utf-8").read())

    def test_dsh_argv(self):
        self.assertEqual(profile.dsh_argv("node", "/d/bin.js", "/p.yml"),
                         ["node", "/d/bin.js", "--profile", "sdk", "--patch", "/p.yml"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_profile.py`
Expected: `ModuleNotFoundError: No module named 'brain.gateway.profile'`

- [ ] **Step 4: 写实现**

```python
# brain/gateway/profile.py
# -*- coding: utf-8 -*-
"""渲染 dsh sdk profile 的 patch 覆盖层，准备一个干净的 DSH_HOME。

为什么不用 !!js 在 yaml 里读文件：patch 由我们渲染更可控，也不依赖 dsh 的表达式求值上下文。
DSH_HOME 必须是大脑专用的空目录：dsh 会读 $DSH_HOME/AGENTS.md 和 $DSH_HOME/skills，
用本机 ~/.dsh 会把别的项目的说明带进肥肉的脑子（2026-09-05 实测踩到）。
"""
from __future__ import annotations
import os
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TMPL_DIR = os.path.join(os.path.dirname(HERE), "profile")

_GROK_BLOCK = """    - id: mcp-grok
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: grok
        transport: stdio
        command: __NODE__
        args:
          - __SERVER_JS__
        env:
          SONGKEY_API_KEY: __KEY__
        toolCallTimeoutMs: 120000
        failOnStartupError: false
"""


def _read(name: str) -> str:
    with open(os.path.join(TMPL_DIR, name), encoding="utf-8") as f:
        return f.read()


def render_patch(persona: str, feirou_mcp_argv: List[str], grok_server_js: Optional[str],
                 songkey_key: str, node: str = "node") -> str:
    tmpl = _read("cordis.patch.yml.tmpl")
    persona_block = "\n".join("      " + ln for ln in persona.strip("\n").split("\n"))
    args_block = "\n".join("          - " + a for a in feirou_mcp_argv[1:])
    grok = ""
    if grok_server_js:
        grok = (_GROK_BLOCK.replace("__NODE__", node).replace("__SERVER_JS__", grok_server_js)
                .replace("__KEY__", songkey_key))
    return (tmpl.replace("__PERSONA__", persona_block)
            .replace("__FEIROU_CMD__", feirou_mcp_argv[0])
            .replace("__FEIROU_ARGS__", args_block)
            .replace("__GROK_BLOCK__\n", grok))


def prepare_dsh_home(dsh_home: str, songkey_key: str, model: str) -> None:
    os.makedirs(dsh_home, exist_ok=True)
    settings = os.path.join(dsh_home, "settings.yaml")
    if not os.path.exists(settings):
        with open(settings, "w", encoding="utf-8") as f:
            f.write(_read("settings.yaml.tmpl").replace("__MODEL__", model))


def dsh_argv(node: str, dsh_bin: str, patch_path: str) -> List[str]:
    return [node, dsh_bin, "--profile", "sdk", "--patch", patch_path]
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_profile.py`
Expected: `OK`（4 个用例）

- [ ] **Step 6: 写期 0 的三个验证脚本**

```python
# brain/verify/echo_mcp.py
# -*- coding: utf-8 -*-
"""期 0 用的最小 MCP stdio 服务：一个 echo 工具。只依赖标准库。"""
import json
import sys

TOOLS = [{"name": "echo", "description": "原样返回 text。测试用。",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    meth, i, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": p.get("protocolVersion", "2025-03-26"),
                                                    "capabilities": {"tools": {}},
                                                    "serverInfo": {"name": "echo", "version": "0"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}})
    elif meth == "tools/call":
        text = (p.get("arguments") or {}).get("text", "")
        send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": "ECHO:" + text}]}})
    elif meth == "ping":
        send({"jsonrpc": "2.0", "id": i, "result": {}})
    elif i is not None:
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": meth}})
```

```python
# brain/verify/p0_resume.py
# -*- coding: utf-8 -*-
"""期 0 验证 ①：dsh sdk 重启后，同一个 sessionId 是否接续上文。

用法：SONGKEY_API_KEY=... python3 brain/verify/p0_resume.py
判据：第二个进程里问"我叫什么"，答案含"松爸" → 接续 OK；否则网关必须自己预热（见设计 §4.5）。
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from brain.gateway import profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402

NODE = os.path.expanduser("~/.nvm/versions/node/v24.19.0/bin/node")
DSH_BIN = os.path.expanduser("~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
KEY = os.environ["SONGKEY_API_KEY"]
MODEL = os.environ.get("MODEL", "songkey-auto")

root = tempfile.mkdtemp(prefix="p0resume_")
ws = os.path.join(root, "ws"); os.makedirs(ws)
home = os.path.join(root, "dsh-home")
profile.prepare_dsh_home(home, KEY, MODEL)
patch = os.path.join(root, "patch.yml")
open(patch, "w", encoding="utf-8").write(profile.render_patch(
    "你是测试助手，用一句话中文回答。", [sys.executable, os.path.join(os.path.dirname(__file__), "echo_mcp.py")], None, KEY))
env = dict(os.environ, DSH_HOME=home, SONGKEY_API_KEY=KEY)


def boot():
    c = DshClient(profile.dsh_argv(NODE, DSH_BIN, patch), cwd=ws, env=env, stderr_path=os.path.join(root, "dsh.err"))
    c.start(); c.initialize(ws, "songkey", MODEL)
    return c


c = boot()
r1 = c.prompt("resume-test", "记住：我叫松爸。回一句话确认。", 90)
print("turn1:", r1.text, "| timed_out:", r1.timed_out)
c.stop()

c = boot()
r2 = c.prompt("resume-test", "我叫什么？一句话。", 90)
print("turn2 (after restart):", r2.text, "| timed_out:", r2.timed_out)
c.stop()
print("RESULT:", "RESUME_OK" if "松爸" in r2.text else "RESUME_LOST", "| sessions dir:", os.listdir(os.path.join(home, "sessions")))
```

```python
# brain/verify/p0_tools.py
# -*- coding: utf-8 -*-
"""期 0 验证 ②：指定模型调 MCP 工具的成功率（10 次），以及 patch 是否真把 bash 等工具关掉了。

用法：SONGKEY_API_KEY=... MODEL=songkey-auto python3 brain/verify/p0_tools.py
判据：ok >= 9/10 才能用该模型当主用；tools 列表里不得出现 bash。
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from brain.gateway import profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402

NODE = os.path.expanduser("~/.nvm/versions/node/v24.19.0/bin/node")
DSH_BIN = os.path.expanduser("~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js")
KEY = os.environ["SONGKEY_API_KEY"]
MODEL = os.environ.get("MODEL", "songkey-auto")
N = int(os.environ.get("N", "10"))

root = tempfile.mkdtemp(prefix="p0tools_")
ws = os.path.join(root, "ws"); os.makedirs(ws)
home = os.path.join(root, "dsh-home")
profile.prepare_dsh_home(home, KEY, MODEL)
patch = os.path.join(root, "patch.yml")
open(patch, "w", encoding="utf-8").write(profile.render_patch(
    "你是测试助手。用户要你调用工具时必须真的调用工具，不要口头假装。",
    [sys.executable, os.path.join(os.path.dirname(__file__), "echo_mcp.py")], None, KEY))
env = dict(os.environ, DSH_HOME=home, SONGKEY_API_KEY=KEY)

# patch 生效检查：dump-config 里 tool-bash 必须 disabled: true
dump = subprocess.run([NODE, DSH_BIN, "--profile", "sdk", "--patch", patch, "--dump-config"],
                      env=env, cwd=ws, capture_output=True, text=True, timeout=120).stdout
i = dump.find("id: tool-bash")
print("PATCH tool-bash block:", dump[i:i + 80].replace("\n", " | "))
print("PATCH persona present:", "你是测试助手" in dump)

c = DshClient(profile.dsh_argv(NODE, DSH_BIN, patch), cwd=ws, env=env, stderr_path=os.path.join(root, "dsh.err"))
c.start(); c.initialize(ws, "songkey", MODEL)
r = c.prompt("list", "列出你现在能用的全部工具名，逗号分隔，不要解释。", 90)
print("TOOLS:", r.text.replace("\n", " ")[:400])
ok = 0
for k in range(N):
    t0 = time.time()
    r = c.prompt(f"t{k}", f"调用 echo 工具，text 参数填 ping{k}，然后把工具返回的原文告诉我。", 90)
    hit = f"ECHO:ping{k}" in r.text or f"ECHO:ping{k}" in str(r.events)
    ok += hit
    print(f"#{k} {'ok ' if hit else 'BAD'} {time.time()-t0:.1f}s text={r.text[:60]!r} timed_out={r.timed_out}")
c.stop()
print(f"RESULT: model={MODEL} tool_calls_ok={ok}/{N}")
```

- [ ] **Step 7: 跑期 0 验证并把结论写进设计文档**

Run（key 从 mac 本机 dsh 的凭据里取，别贴进代码）：
```bash
cd /Volumes/SiverWXbot_plus-main && export SONGKEY_API_KEY=$(grep -o 'sk-[A-Za-z0-9]*' ~/.dsh/profiles/web/cordis.patch.yml | head -1) && python3 brain/verify/p0_resume.py 2>&1 | tail -5
```
Expected: 最后一行 `RESULT: RESUME_OK ...` 或 `RESULT: RESUME_LOST ...`（两种都是合法结论）

```bash
cd /Volumes/SiverWXbot_plus-main && export SONGKEY_API_KEY=$(grep -o 'sk-[A-Za-z0-9]*' ~/.dsh/profiles/web/cordis.patch.yml | head -1) && MODEL=songkey-auto python3 brain/verify/p0_tools.py 2>&1 | tail -15
```
Expected: `PATCH tool-bash block: id: tool-bash | disabled: true ...`、`PATCH persona present: True`、`TOOLS:` 里没有 bash、最后 `RESULT: model=songkey-auto tool_calls_ok=N/10`。
若 N < 9，再跑一次 `MODEL=deepseek-v4-flash`，取成功率高者。

把三条结论（接续 / patch / 工具成功率与选定模型）追加到 `docs/superpowers/specs/2026-09-05-dsh-brain-design.md` 的 §9 期 0 那一行后面，格式：`期 0 结论（日期）：① … ② … ③ …`。若接续结论是 LOST，Task 8 的 `Gateway._prime_if_new` 就是必须而不是兜底。

- [ ] **Step 8: 提交**

```bash
git add brain/profile brain/gateway/profile.py brain/verify tests/test_brain_profile.py docs/superpowers/specs/2026-09-05-dsh-brain-design.md
git commit -m "feat(brain): sdk profile patch 模板 + DSH_HOME 准备 + 期 0 验证脚本与结论"
```

---

### Task 4: MCP 哑桥 `feirou_tools.py`

**Files:**
- Create: `brain/mcp/__init__.py`, `brain/mcp/feirou_tools.py`
- Test: `tests/test_brain_mcp.py`

**Interfaces:**
- Produces: stdio MCP 服务，暴露 4 个工具 `wx_reply(bubbles: string[])`、`no_reply(reason: string)`、`propose_shared_knowledge(text: string, source: string)`、`kb_search(query: string)`；每次 `tools/call` 都 `POST {GATEWAY}/tool/<name>`，body 是 arguments JSON，网关返回 `{"ok": bool, "text": str}`，桥把 `text` 原样作为工具结果返回，`ok=false` 时 `isError=true`。
- Consumes: 环境变量 `FEIROU_GW`（默认 `http://127.0.0.1:8500`）。
- dsh 侧工具名会变成 `mcp__feirou__wx_reply` 等（dsh-mcp-client 的命名规则）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_mcp.py
# -*- coding: utf-8 -*-
"""起一个假网关 HTTP，把 feirou_tools.py 当子进程跑，走一遍 initialize / tools/list / tools/call。"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLS = []


class FakeGw(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        CALLS.append((self.path, body))
        ok = self.path != "/tool/no_reply"
        out = json.dumps({"ok": ok, "text": "gw saw " + self.path}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


class McpBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeGw)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        env = dict(os.environ, FEIROU_GW=f"http://127.0.0.1:{cls.srv.server_port}")
        script = os.path.join(os.path.dirname(__file__), "..", "brain", "mcp", "feirou_tools.py")
        cls.p = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True, encoding="utf-8", bufsize=1, env=env)

    @classmethod
    def tearDownClass(cls):
        cls.p.kill(); cls.srv.shutdown()

    def rpc(self, i, method, params=None):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def test_handshake_list_call(self):
        r = self.rpc(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "feirou")
        r = self.rpc(2, "tools/list")
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["wx_reply", "no_reply", "propose_shared_knowledge", "kb_search"])
        r = self.rpc(3, "tools/call", {"name": "wx_reply", "arguments": {"bubbles": ["hi"]}})
        self.assertEqual(r["result"]["content"][0]["text"], "gw saw /tool/wx_reply")
        self.assertFalse(r["result"].get("isError", False))
        self.assertEqual(CALLS[-1], ("/tool/wx_reply", {"bubbles": ["hi"]}))
        r = self.rpc(4, "tools/call", {"name": "no_reply", "arguments": {"reason": "x"}})
        self.assertTrue(r["result"]["isError"])

    def test_unknown_tool_is_error(self):
        r = self.rpc(9, "tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(r["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_mcp.py`
Expected: FAIL（子进程起不来 / readline 拿到空）

- [ ] **Step 3: 写实现**

```python
# brain/mcp/__init__.py
```

```python
# brain/mcp/feirou_tools.py
# -*- coding: utf-8 -*-
"""肥肉大脑的 MCP 哑桥：4 个工具，逻辑全在网关，这里只转发。

为什么是哑桥：校验（预算/气泡数/反口头禅）需要知道"当前在回哪条消息"，只有网关知道；
MCP 服务由 dsh 拉起、跨会话共用，自己没法知道。所以这里一行业务逻辑都不能有。
stdout 只能写 JSON-RPC 帧；调试信息写 stderr。
"""
import json
import os
import sys
import urllib.request

GW = os.environ.get("FEIROU_GW", "http://127.0.0.1:8500").rstrip("/")

TOOLS = [
    {"name": "wx_reply",
     "description": "把你决定要说的话发到微信。这是唯一的说话通道，正文里写的字不会被发出去。"
                    "群聊最多 2 条气泡、私聊最多 3 条，总字数要在系统给你的预算之内；像真人一样一两句话，不要客套收尾。"
                    "被拒绝时按返回的提示改了再调一次。",
     "inputSchema": {"type": "object", "properties": {
         "bubbles": {"type": "array", "items": {"type": "string"}, "description": "按顺序发出的气泡，每条一段话"}},
         "required": ["bubbles"]}},
    {"name": "no_reply",
     "description": "判断这条消息不需要接话时调用（附和、点赞、别人之间的闲聊、话题已经聊完）。调了它就不要再调 wx_reply。",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "propose_shared_knowledge",
     "description": "把聊天里得到的、对所有人都有用的事实（据点变动、价格、联系方式、活动）提交审核。"
                    "通过后才会进入共享知识；未通过前不要当作事实告诉别人。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "一句话事实"},
         "source": {"type": "string", "description": "谁在哪说的"}}, "required": ["text", "source"]}},
    {"name": "kb_search",
     "description": "检索 NCC 社区知识库（公众号文章 + 固定事实清单）。问据点、活动、报名、主理人、社区历史时先查再答。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]
NAMES = {t["name"] for t in TOOLS}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call_gateway(name, args):
    req = urllib.request.Request(f"{GW}/tool/{name}", data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        meth, i, p = m.get("method"), m.get("id"), m.get("params") or {}
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": i, "result": {
                "protocolVersion": p.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "feirou", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}})
        elif meth == "tools/call":
            name, args = p.get("name"), p.get("arguments") or {}
            if name not in NAMES:
                send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True}})
                continue
            try:
                res = call_gateway(name, args)
                ok, text = bool(res.get("ok")), str(res.get("text", ""))
            except Exception as e:  # 网关不在 → 告诉模型，别让 dsh 挂
                ok, text = False, f"网关不可用：{e}"
            send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": text}], "isError": not ok}})
        elif meth == "ping":
            send({"jsonrpc": "2.0", "id": i, "result": {}})
        elif i is not None:
            send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": f"method not found: {meth}"}})


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_mcp.py`
Expected: `OK`（2 个用例）

- [ ] **Step 5: 提交**

```bash
git add brain/mcp tests/test_brain_mcp.py
git commit -m "feat(brain): MCP 哑桥 feirou_tools（wx_reply/no_reply/propose/kb_search 全转发网关）"
```

---

### Task 5: 上下文拼装（前缀行、技能匹配、prime 过滤）

**Files:**
- Create: `brain/gateway/context.py`, `brain/workspace/skills/index.json`
- Test: `tests/test_brain_context.py`

**Interfaces:**
- Produces: `context.filter_prime(items: list[dict], count: int) -> list[dict]`（items 是机器人 memory 格式 `{time, type, attr, sender, content}`）
- Produces: `context.match_skills(index: dict, conversation: str, is_group: bool) -> list[str]`
- Produces: `context.build_user_message(conversation, is_group, sender, text, now_str, skills, prime=None) -> str`
- Produces: `context.load_skill_index(workspace_dir) -> dict`

- [ ] **Step 1: 写技能索引种子**

```json
{
  "hz-food-map": {"groups": ["共建杭州美食地图"]},
  "ncc-community": {"scope": "all"}
}
```
（写到 `brain/workspace/skills/index.json`）

- [ ] **Step 2: 写失败的测试**

```python
# tests/test_brain_context.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway import context

IDX = {"hz-food-map": {"groups": ["共建杭州美食地图"]}, "ncc-community": {"scope": "all"}}


class SkillsTest(unittest.TestCase):
    def test_group_specific_plus_all(self):
        self.assertEqual(context.match_skills(IDX, "共建杭州美食地图", True), ["hz-food-map", "ncc-community"])

    def test_other_group_only_all(self):
        self.assertEqual(context.match_skills(IDX, "肥肉测试1🐶", True), ["ncc-community"])


class PrimeTest(unittest.TestCase):
    def test_drops_system_and_checkin(self):
        items = [
            {"time": "2026/08/03 19:00:00", "type": "time", "attr": "system", "sender": "system", "content": "19:00"},
            {"time": "2026/08/03 19:01:00", "type": "text", "attr": "friend", "sender": "K", "content": "签到！"},
            {"time": "2026/08/03 19:01:05", "type": "text", "attr": "self", "sender": "肥肉", "content": "兑换码 BTC-MF86-GEU6-BEM5，去 key.bigsong.site 兑换"},
            {"time": "2026/08/03 19:01:30", "type": "text", "attr": "self", "sender": "肥肉", "content": "在忙，我稍后回复您"},
            {"time": "2026/08/03 19:02:00", "type": "text", "attr": "friend", "sender": "K", "content": "大理还开着吗"},
        ]
        out = context.filter_prime(items, 20)
        self.assertEqual([x["content"] for x in out], ["大理还开着吗"])  # 系统时间戳/签到/兑换码/兜底文案全部不进

    def test_is_checkin_text(self):
        self.assertTrue(context.is_checkin_text("签到"))
        self.assertTrue(context.is_checkin_text(" 打卡~ "))
        self.assertFalse(context.is_checkin_text("签到功能是怎么做的"))

    def test_keeps_last_n(self):
        items = [{"time": "t", "type": "text", "attr": "friend", "sender": "a", "content": str(i)} for i in range(30)]
        self.assertEqual([x["content"] for x in context.filter_prime(items, 3)], ["27", "28", "29"])


class MessageTest(unittest.TestCase):
    def test_prefix_line_group(self):
        m = context.build_user_message("共建杭州美食地图", True, "松爸", "吃啥", "2026-09-05 14:02", ["hz-food-map"])
        self.assertEqual(m, "[群聊:共建杭州美食地图 | 发言人:松爸 | 2026-09-05 14:02 | 相关技能:hz-food-map]\n松爸: 吃啥")

    def test_private_with_prime(self):
        prime = [{"time": "2026/09/05 13:00:00", "attr": "friend", "sender": "K", "content": "你好"},
                 {"time": "2026/09/05 13:00:10", "attr": "self", "sender": "肥肉", "content": "汪"}]
        m = context.build_user_message("K", False, "K", "在吗", "2026-09-05 14:02", [], prime=prime)
        self.assertTrue(m.startswith("[私聊:K | 发言人:K | 2026-09-05 14:02 | 相关技能:无]\n"))
        self.assertIn("以下是此前的聊天记录，只供了解背景，不要逐条回复：\n[2026/09/05 13:00:00] K: 你好\n[2026/09/05 13:00:10] 你(肥肉): 汪\n---\n", m)
        self.assertTrue(m.endswith("K: 在吗"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_context.py`
Expected: `ModuleNotFoundError`

- [ ] **Step 4: 写实现**

```python
# brain/gateway/context.py
# -*- coding: utf-8 -*-
"""把一条微信消息变成喂给大脑的用户消息：前缀行 + （首次）历史预热 + 正文。"""
from __future__ import annotations
import json
import os
import re
from typing import List, Optional

from plugins.context_guard.guard import filter_history  # 时间戳条目/兜底文案/[NO_REPLY]/"没法联网"整轮连坐
from plugins.wechat_checkin.handler import TRIGGERS, normalize_text  # 签到触发词的唯一真相源

# 签到往来不进大脑（设计 §2.4）：触发词整条命中（去空白标点后）、兑换码、兑换站点
_CHECKIN_TAIL = re.compile(r"(BTC-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}|key\.bigsong\.site|兑换码)")


def is_checkin_text(text: str) -> bool:
    t = normalize_text(text or "")
    return t in TRIGGERS or bool(_CHECKIN_TAIL.search(text or ""))


def load_skill_index(workspace_dir: str) -> dict:
    path = os.path.join(workspace_dir, "skills", "index.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def match_skills(index: dict, conversation: str, is_group: bool) -> List[str]:
    out = []
    for name, meta in index.items():
        if meta.get("scope") == "all":
            continue
        if is_group and conversation in (meta.get("groups") or []):
            out.append(name)
        if not is_group and conversation in (meta.get("chats") or []):
            out.append(name)
    out += [n for n, m in index.items() if m.get("scope") == "all"]
    return out


def filter_prime(items: List[dict], count: int) -> List[dict]:
    kept = [x for x in filter_history(list(items or []))
            if x.get("attr") in ("friend", "self") and x.get("type", "text") == "text"
            and not is_checkin_text(str(x.get("content", "")))]
    return kept[-count:]


def build_user_message(conversation: str, is_group: bool, sender: str, text: str, now_str: str,
                       skills: List[str], prime: Optional[List[dict]] = None) -> str:
    kind = "群聊" if is_group else "私聊"
    head = f"[{kind}:{conversation} | 发言人:{sender} | {now_str} | 相关技能:{','.join(skills) if skills else '无'}]"
    parts = [head]
    if prime:
        lines = []
        for x in prime:
            who = f"你({x.get('sender')})" if x.get("attr") == "self" else x.get("sender")
            lines.append(f"[{x.get('time')}] {who}: {x.get('content')}")
        parts.append("以下是此前的聊天记录，只供了解背景，不要逐条回复：\n" + "\n".join(lines) + "\n---")
    parts.append(f"{sender}: {text}")
    return "\n".join(parts)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_context.py`
Expected: `OK`（7 个用例）

- [ ] **Step 6: 提交**

```bash
git add brain/gateway/context.py brain/workspace/skills/index.json tests/test_brain_context.py
git commit -m "feat(brain): 上下文拼装（前缀行 / 技能匹配 / 预热过滤签到往来）"
```

---

### Task 6: 最近回复记录 + 回复校验

**Files:**
- Create: `brain/gateway/replies.py`, `brain/gateway/validate.py`
- Test: `tests/test_brain_replies.py`, `tests/test_brain_validate.py`

**Interfaces:**
- Consumes: `shape.*`（Task 1）
- Produces: `class RecentReplies(path, global_n, per_conv_n)`：`add(conversation, bubbles)`, `recent_for(conversation) -> list[str]`（本会话 + 全局去重），落盘 JSON，构造时加载。
- Produces: `validate.validate_reply(bubbles, is_group, budget, recent, attempt, cfg) -> tuple[Optional[list[str]], Optional[str]]`：`(accepted, None)` 或 `(None, 退回理由)`。attempt 从 1 起；第 2 次起超预算改截断、重复改放行。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_replies.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import tempfile
import unittest
from brain.gateway.replies import RecentReplies


class RecentRepliesTest(unittest.TestCase):
    def test_add_and_recent_merges_conv_and_global(self):
        p = os.path.join(tempfile.mkdtemp(), "recent.json")
        r = RecentReplies(p, global_n=3, per_conv_n=2)
        r.add("A", ["a1", "a2", "a3"])
        r.add("B", ["b1"])
        self.assertEqual(r.recent_for("A"), ["a2", "a3", "b1"])
        self.assertEqual(r.recent_for("C"), ["a2", "a3", "b1"])

    def test_persists(self):
        p = os.path.join(tempfile.mkdtemp(), "recent.json")
        RecentReplies(p, 5, 5).add("A", ["x"])
        self.assertEqual(RecentReplies(p, 5, 5).recent_for("A"), ["x"])


if __name__ == "__main__":
    unittest.main()
```

```python
# tests/test_brain_validate.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway.config import DEFAULTS
from brain.gateway.validate import validate_reply


class ValidateTest(unittest.TestCase):
    def test_accepts_short_reply_and_strips_markdown(self):
        ok, err = validate_reply(["**大理**还开着"], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(err)
        self.assertEqual(ok, ["大理还开着"])

    def test_rejects_empty(self):
        ok, err = validate_reply([" "], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("no_reply", err)

    def test_rejects_too_many_bubbles(self):
        ok, err = validate_reply(["a", "b", "c"], True, 100, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("最多 2 条", err)

    def test_over_budget_first_attempt_rejects_second_truncates(self):
        ok, err = validate_reply(["一" * 60], True, 40, [], 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("预算 40", err)
        ok, err = validate_reply(["一" * 60], True, 40, [], 2, DEFAULTS)
        self.assertIsNone(err); self.assertEqual(ok, ["一" * 40])

    def test_repeat_first_attempt_rejects_second_passes(self):
        recent = ["我刚从键盘上趴起来，刷到消息"]
        ok, err = validate_reply(["我刚从键盘上趴起来，看到你"], False, 100, recent, 1, DEFAULTS)
        self.assertIsNone(ok); self.assertIn("重复", err)
        ok, err = validate_reply(["我刚从键盘上趴起来，看到你"], False, 100, recent, 2, DEFAULTS)
        self.assertIsNone(err)

    def test_strips_closing_question(self):
        ok, err = validate_reply(["大理还开着。", "要不要我把地址发你？"], True, 100, [], 1, DEFAULTS)
        self.assertEqual(ok, ["大理还开着。"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_replies.py; PYTHONPATH=. python3 tests/test_brain_validate.py`
Expected: 两个都 `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# brain/gateway/replies.py
# -*- coding: utf-8 -*-
"""最近已发气泡：给反口头禅用。全局一份 + 每会话一份，落盘 JSON。"""
from __future__ import annotations
import json
import os
import threading
from collections import deque
from typing import Dict, List


class RecentReplies:
    def __init__(self, path: str, global_n: int, per_conv_n: int):
        self.path, self.gn, self.cn = path, global_n, per_conv_n
        self.g: deque = deque(maxlen=global_n)
        self.c: Dict[str, deque] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.g.extend(d.get("global", []))
        for k, v in (d.get("conversations") or {}).items():
            self.c[k] = deque(v, maxlen=self.cn)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"global": list(self.g), "conversations": {k: list(v) for k, v in self.c.items()}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, conversation: str, bubbles: List[str]) -> None:
        with self._lock:
            dq = self.c.setdefault(conversation, deque(maxlen=self.cn))
            for b in bubbles:
                dq.append(b)
                self.g.append(b)
            self._save()

    def recent_for(self, conversation: str) -> List[str]:
        with self._lock:
            seen, out = set(), []
            for b in list(self.c.get(conversation, [])) + list(self.g):
                if b not in seen:
                    seen.add(b)
                    out.append(b)
            return out
```

```python
# brain/gateway/validate.py
# -*- coding: utf-8 -*-
"""wx_reply 的接受/退回决定。attempt=1 退回让模型改，attempt>=2 强制收口（截断/放行）。"""
from __future__ import annotations
from typing import List, Optional, Tuple

from . import shape


def validate_reply(bubbles: List[str], is_group: bool, budget: int, recent: List[str],
                   attempt: int, cfg: dict) -> Tuple[Optional[List[str]], Optional[str]]:
    bubbles = [shape.strip_markdown(b).strip() for b in (bubbles or []) if isinstance(b, str)]
    bubbles = [b for b in bubbles if b]
    if not bubbles:
        return None, "bubbles 为空。要么给至少一条有内容的话，要么调用 no_reply。"
    mb = shape.max_bubbles(is_group, cfg)
    if len(bubbles) > mb:
        return None, f"最多 {mb} 条气泡，你给了 {len(bubbles)} 条。合并或删掉最不重要的。"
    total = sum(shape.text_len(b) for b in bubbles)
    if total > budget:
        if attempt < 2:
            return None, f"总字数 {total} 超过预算 {budget}。压到 {budget} 字以内，只留最有信息量的话，去掉铺垫和客套。"
        bubbles = shape.truncate_to(bubbles, budget)
    if attempt < 2:
        for b in bubbles:
            hit = shape.repeats(b, recent, cfg.get("repeat_threshold", 0.5))
            if hit:
                return None, f"「{b[:12]}…」和你之前说过的「{hit[:12]}…」开头或措辞重复了。换个说法，别用同一个开场白。"
    bubbles = shape.strip_closing(bubbles)
    return bubbles, None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_replies.py; PYTHONPATH=. python3 tests/test_brain_validate.py`
Expected: `OK`（2 个 + 6 个）

- [ ] **Step 5: 提交**

```bash
git add brain/gateway/replies.py brain/gateway/validate.py tests/test_brain_replies.py tests/test_brain_validate.py
git commit -m "feat(brain): 最近回复记录 + wx_reply 校验（气泡数/预算/反口头禅/剥收尾）"
```

---

### Task 7: 共享知识提议 + 记忆文件守卫 + 知识库客户端

**Files:**
- Create: `brain/gateway/proposals.py`, `brain/gateway/memory_guard.py`, `brain/gateway/kb.py`
- Test: `tests/test_brain_proposals.py`

**Interfaces:**
- Produces: `class Proposals(workspace_dir)`：`create(text, source, conversation) -> str(id)`, `list(status=None) -> list[dict]`, `approve(id) -> dict`, `reject(id) -> dict`。approve 追加到 `<workspace>/knowledge/shared.md` 一行 `- (YYYY-MM-DD，来源: source) text`。
- Produces: `memory_guard.enforce(memory_dir, max_bytes) -> list[str]`（被截尾的文件路径）
- Produces: `kb.search(kb_url, query, timeout) -> str`（给模型看的文本；失败返回以「检索不可用」开头的一句话）。调 `POST {kb_url}/retrieve`，body `{"query": q}`，响应 `{context, is_ncc, facts, meta}`（Task 9 在 mac-mini 上加这个端点）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_proposals.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from brain.gateway import kb, memory_guard
from brain.gateway.proposals import Proposals


class ProposalsTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "knowledge"))
        open(os.path.join(self.ws, "knowledge", "shared.md"), "w", encoding="utf-8").write("# 共享知识\n")
        self.p = Proposals(self.ws)

    def test_create_list_approve(self):
        pid = self.p.create("大理下月涨到 3000", "松爸在私聊说的", "松爸")
        self.assertEqual([x["id"] for x in self.p.list("pending")], [pid])
        self.p.approve(pid)
        self.assertEqual(self.p.list("pending"), [])
        shared = open(os.path.join(self.ws, "knowledge", "shared.md"), encoding="utf-8").read()
        self.assertIn("来源: 松爸在私聊说的) 大理下月涨到 3000", shared)

    def test_reject_does_not_touch_shared(self):
        pid = self.p.create("x", "y", "z")
        self.p.reject(pid)
        self.assertEqual(self.p.list("rejected")[0]["id"], pid)
        self.assertNotIn("x", open(os.path.join(self.ws, "knowledge", "shared.md"), encoding="utf-8").read())


class MemoryGuardTest(unittest.TestCase):
    def test_truncates_oversized(self):
        d = tempfile.mkdtemp()
        big = os.path.join(d, "松爸.md")
        open(big, "w", encoding="utf-8").write("行\n" * 3000)
        small = os.path.join(d, "小.md")
        open(small, "w", encoding="utf-8").write("ok")
        out = memory_guard.enforce(d, 4096)
        self.assertEqual(out, [big])
        data = open(big, "rb").read()
        self.assertLessEqual(len(data), 4096 + 40)
        self.assertTrue(data.decode("utf-8").endswith("[已截尾]\n"))


class KbTest(unittest.TestCase):
    def test_search_formats_context_and_facts(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
                out = json.dumps({"context": "[1] 大理据点在古城", "is_ncc": True, "facts": "自营：大理、黄山",
                                  "meta": {"trigger": "关键词"}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        txt = kb.search(f"http://127.0.0.1:{srv.server_port}", "大理", 5)
        srv.shutdown()
        self.assertTrue(txt.startswith("【固定事实清单（优先级最高）】\n自营：大理、黄山"))
        self.assertIn("【检索片段】\n[1] 大理据点在古城", txt)

    def test_search_unavailable(self):
        txt = kb.search("http://127.0.0.1:9", "x", 1)
        self.assertTrue(txt.startswith("检索不可用"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_proposals.py`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# brain/gateway/proposals.py
# -*- coding: utf-8 -*-
"""共享知识提议：大脑只能 create，approve/reject 由面板（人）调。approve 才写 shared.md。"""
from __future__ import annotations
import json
import os
import threading
import time
import uuid
from typing import List, Optional


class Proposals:
    def __init__(self, workspace_dir: str):
        self.dir = os.path.join(workspace_dir, "proposals")
        self.shared = os.path.join(workspace_dir, "knowledge", "shared.md")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, pid: str) -> str:
        return os.path.join(self.dir, pid + ".json")

    def _write(self, rec: dict) -> None:
        with open(self._path(rec["id"]), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)

    def _read(self, pid: str) -> dict:
        with open(self._path(pid), encoding="utf-8") as f:
            return json.load(f)

    def create(self, text: str, source: str, conversation: str) -> str:
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self._lock:
            self._write({"id": pid, "text": text.strip(), "source": source.strip(), "conversation": conversation,
                         "status": "pending", "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        return pid

    def list(self, status: Optional[str] = None) -> List[dict]:
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".json"):
                rec = self._read(name[:-5])
                if status is None or rec["status"] == status:
                    out.append(rec)
        return out

    def approve(self, pid: str) -> dict:
        with self._lock:
            rec = self._read(pid)
            if rec["status"] != "approved":
                with open(self.shared, "a", encoding="utf-8") as f:
                    f.write(f"- ({time.strftime('%Y-%m-%d')}，来源: {rec['source']}) {rec['text']}\n")
                rec["status"] = "approved"
                rec["decided"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._write(rec)
            return rec

    def reject(self, pid: str) -> dict:
        with self._lock:
            rec = self._read(pid)
            rec["status"] = "rejected"
            rec["decided"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write(rec)
            return rec
```

```python
# brain/gateway/memory_guard.py
# -*- coding: utf-8 -*-
"""记忆文件上限：超过 max_bytes 的 .md 按行截尾，末尾打标记。递归扫 memory_dir。"""
from __future__ import annotations
import os
from typing import List

MARK = "\n[已截尾]\n"


def enforce(memory_dir: str, max_bytes: int) -> List[str]:
    hit = []
    for root, _dirs, files in os.walk(memory_dir):
        for name in files:
            if not name.endswith(".md"):
                continue
            path = os.path.join(root, name)
            data = open(path, "rb").read()
            if len(data) <= max_bytes:
                continue
            head = data[:max_bytes].decode("utf-8", errors="ignore")
            head = head[:head.rfind("\n")] if "\n" in head else head
            with open(path, "w", encoding="utf-8") as f:
                f.write(head + MARK)
            hit.append(path)
    return hit
```

```python
# brain/gateway/kb.py
# -*- coding: utf-8 -*-
"""NCC 知识库只读客户端：POST {kb_url}/retrieve。失败返回一句"检索不可用"，让模型按不知道处理。"""
from __future__ import annotations
import json
import urllib.request


def search(kb_url: str, query: str, timeout: float) -> str:
    try:
        req = urllib.request.Request(kb_url.rstrip("/") + "/retrieve",
                                     data=json.dumps({"query": query}, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return f"检索不可用（{type(e).__name__}）。不要编造，按不知道处理，建议对方问群里的主理人。"
    parts = []
    if d.get("facts"):
        parts.append("【固定事实清单（优先级最高）】\n" + d["facts"].strip())
    if d.get("context"):
        parts.append("【检索片段】\n" + d["context"].strip())
    if not parts:
        return "知识库里没有相关内容。不要编造。"
    return "\n\n".join(parts)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_proposals.py`
Expected: `OK`（5 个用例）

- [ ] **Step 5: 提交**

```bash
git add brain/gateway/proposals.py brain/gateway/memory_guard.py brain/gateway/kb.py tests/test_brain_proposals.py
git commit -m "feat(brain): 共享知识提议 / 记忆文件守卫 / 知识库只读客户端"
```

---

### Task 8: 网关本体（Gateway + HTTP）、工作区种子、入口

**Files:**
- Create: `brain/gateway/server.py`, `brain/gateway/compat.py`, `brain/run.py`, `brain/workspace/PERSONA.md`, `brain/workspace/knowledge/shared.md`, `brain/workspace/skills/hz-food-map/SKILL.md`, `brain/workspace/skills/ncc-community/SKILL.md`, `brain/mcp/grok_search/server.js`
- Test: `tests/test_brain_server.py`, `tests/test_brain_compat.py`

**Interfaces:**
- Consumes: Task 1–7 全部。
- Produces: `compat.parse_model(model) -> (conversation, is_group)`（`feirou:group:X` / `feirou:chat:X`，其它模型名当私聊、会话名=模型名）、`compat.parse_last_user(messages) -> (sender, text)`（`昵称: 内容` 或 `[时间] 昵称: 内容`，没有前缀时 sender=""）、`compat.history_to_prime(messages) -> list[dict]`（user→friend、assistant→self，最后一条 user 不算）、`compat.to_completion(result, model) -> dict`（bubbles 用 `||SPLIT||` 拼；no_reply 返回 `[NO_REPLY]`；error 返回机器人认识的失败串）。
- Produces: `class Gateway(cfg, data_dir, workspace_dir, dsh_factory)`：`handle_reply(payload) -> dict`、`tool_call(name, args) -> dict{ok,text}`、`state()`；`serve(gateway, bind, port)` 起 ThreadingHTTPServer。
- HTTP：`POST /reply`、`POST /tool/<name>`、`GET /proposals`、`POST /proposals/<id>/approve|reject`、`GET /log?limit=50`、`GET /health`、`GET /skills`。
- `dsh_factory()` 返回一个有 `start/initialize/prompt/alive/stop` 的对象（生产是 DshClient，测试是假的）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_server.py
# -*- coding: utf-8 -*-
"""Gateway 的一轮完整流程：prompt → 模型（假）调 wx_reply → 校验 → 返回。不起真 dsh。"""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from brain.gateway.config import DEFAULTS
from brain.gateway.dsh_client import TurnResult
from brain.gateway.server import Gateway


class FakeDsh:
    """prompt 时按脚本回调网关的 tool_call，模拟模型调工具。"""
    def __init__(self, gw_ref):
        self.gw_ref = gw_ref
        self.script = []          # list of callables(gw) -> None
        self.prompts = []
        self._alive = True

    def start(self): pass
    def initialize(self, cwd, provider, model): return {}
    def alive(self): return self._alive
    def stop(self): self._alive = False

    def prompt(self, session_id, text, timeout_sec):
        self.prompts.append((session_id, text))
        if self.script:
            self.script.pop(0)(self.gw_ref[0])
        return TurnResult(reasoning="想了一下", text="草稿：不会被发出去", events=[{"type": "turn/end"}])


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.ws = os.path.join(self.data, "workspace")
        os.makedirs(os.path.join(self.ws, "knowledge")); os.makedirs(os.path.join(self.ws, "memory", "people"))
        open(os.path.join(self.ws, "knowledge", "shared.md"), "w").write("# 共享知识\n")
        os.makedirs(os.path.join(self.ws, "skills"))
        json.dump({"ncc-community": {"scope": "all"}}, open(os.path.join(self.ws, "skills", "index.json"), "w"))
        ref = []
        self.fake = FakeDsh(ref)
        self.gw = Gateway(DEFAULTS, self.data, self.ws, dsh_factory=lambda: self.fake)
        ref.append(self.gw)

    def test_reply_roundtrip(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["大理还开着，黄山也在。"]})]
        out = self.gw.handle_reply({"conversation": "肥肉测试1🐶", "is_group": True, "sender": "松爸", "text": "大理还开吗"})
        self.assertEqual(out["bubbles"], ["大理还开着，黄山也在。"])
        self.assertEqual(self.fake.prompts[0][0], "肥肉测试1🐶")
        self.assertIn("[群聊:肥肉测试1🐶 | 发言人:松爸", self.fake.prompts[0][1])
        log = open(os.path.join(self.data, "log", "replies-" + __import__("time").strftime("%Y%m%d") + ".jsonl"), encoding="utf-8").read()
        self.assertIn("大理还开着", log)

    def test_no_reply(self):
        self.fake.script = [lambda gw: gw.tool_call("no_reply", {"reason": "附和"})]
        out = self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "哈哈"})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "附和")

    def test_rejected_then_accepted(self):
        def first(gw):
            r = gw.tool_call("wx_reply", {"bubbles": ["一" * 100]})
            assert not r["ok"] and "预算" in r["text"]
            r2 = gw.tool_call("wx_reply", {"bubbles": ["短的。"]})
            assert r2["ok"]
        self.fake.script = [first]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["短的。"])

    def test_silent_model_gets_nudge_then_no_reply(self):
        self.fake.script = [lambda gw: None, lambda gw: None]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "model_silent")
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn("wx_reply", self.fake.prompts[1][1])

    def test_prime_only_first_time(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["在"]}),
                            lambda gw: gw.tool_call("wx_reply", {"bubbles": ["还在"]})]
        prime = [{"time": "t", "attr": "friend", "sender": "K", "content": "早", "type": "text"}]
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "在吗", "prime": prime})
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "还在吗", "prime": prime})
        self.assertIn("此前的聊天记录", self.fake.prompts[0][1])
        self.assertNotIn("此前的聊天记录", self.fake.prompts[1][1])

    def test_empty_text_is_no_reply_without_touching_dsh(self):
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "  "})
        self.assertTrue(out["no_reply"]); self.assertEqual(out["reason"], "empty")
        self.assertEqual(self.fake.prompts, [])

    def test_openai_compat_roundtrip(self):
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["西湖边那家", "松爸推荐的"]})]
        out = self.gw.handle_completion({"model": "feirou:group:共建杭州美食地图", "messages": [
            {"role": "system", "content": "人设（会被忽略）"},
            {"role": "user", "content": "小A: 上次那家咖啡店叫啥"},
            {"role": "assistant", "content": "记不清了"},
            {"role": "user", "content": "松爸: 西湖附近有啥推荐"}]})
        self.assertEqual(out["choices"][0]["message"]["content"], "西湖边那家||SPLIT||松爸推荐的")
        self.assertEqual(self.fake.prompts[0][0], "共建杭州美食地图")
        self.assertIn("发言人:松爸", self.fake.prompts[0][1])
        self.assertIn("此前的聊天记录", self.fake.prompts[0][1])
        self.assertIn("小A: 上次那家咖啡店叫啥", self.fake.prompts[0][1])

    def test_tool_call_outside_request(self):
        r = self.gw.tool_call("wx_reply", {"bubbles": ["x"]})
        self.assertFalse(r["ok"])

    def test_propose_creates_pending(self):
        self.fake.script = [lambda gw: (gw.tool_call("propose_shared_knowledge", {"text": "大理涨价", "source": "K说"}),
                                        gw.tool_call("no_reply", {"reason": "记下了"}))]
        self.gw.handle_reply({"conversation": "K", "is_group": False, "sender": "K", "text": "大理涨价了"})
        self.assertEqual(self.gw.proposals.list("pending")[0]["text"], "大理涨价")

    def test_dsh_crash_restarts(self):
        self.fake._alive = False
        self.fake.script = [lambda gw: gw.tool_call("wx_reply", {"bubbles": ["ok"]})]
        out = self.gw.handle_reply({"conversation": "K", "is_group": True, "sender": "K", "text": "嗯"})
        self.assertEqual(out["bubbles"], ["ok"])
        self.assertTrue(self.fake.alive())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_server.py`
Expected: `ModuleNotFoundError: No module named 'brain.gateway.server'`

- [ ] **Step 3: 写 Gateway 与 HTTP**

```python
# brain/gateway/server.py
# -*- coding: utf-8 -*-
"""大脑网关：一次 /reply = 一轮 dsh 对话；模型只能通过工具回调说话。

并发模型（期 1 刻意简单）：全局一把锁，同一时刻只处理一条消息。原因是 MCP 工具回调
不带会话标识，网关靠"当前在飞的那条请求"来归属工具调用。测试群流量小，够用；
以后要并发就给 wx_reply 加 conversation 参数并按在飞集合归属。
"""
from __future__ import annotations
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional
from urllib.parse import urlparse, parse_qs

from . import context, kb, memory_guard, shape
from .proposals import Proposals
from .replies import RecentReplies
from .validate import validate_reply

NUDGE = "你刚才没有调用 wx_reply 也没有调用 no_reply。现在二选一：要说话就调 wx_reply 把话放进 bubbles；不该接话就调 no_reply。"


@dataclass
class Inflight:
    conversation: str
    is_group: bool
    budget: int
    attempts: int = 0
    result: Optional[dict] = None          # {"bubbles": [...]} 或 {"no_reply": True, "reason": ...}
    tool_log: List[dict] = field(default_factory=list)


class Gateway:
    def __init__(self, cfg: dict, data_dir: str, workspace_dir: str, dsh_factory: Callable):
        self.cfg, self.data, self.ws = cfg, data_dir, workspace_dir
        self.dsh_factory = dsh_factory
        self.dsh = None
        self.lock = threading.Lock()
        self.inflight: Optional[Inflight] = None
        self.recent = RecentReplies(os.path.join(data_dir, "log", "recent.json"),
                                    cfg["recent_global"], cfg["recent_per_conversation"])
        self.proposals = Proposals(workspace_dir)
        self.skill_index = context.load_skill_index(workspace_dir)
        self.primed = set()
        self.started_at = time.time()
        self.turns = 0

    # ---- dsh 生命周期 ----
    def _ensure_dsh(self):
        if self.dsh is None or not self.dsh.alive():
            self.dsh = self.dsh_factory()
            self.dsh.start()
            self.dsh.initialize(self.ws, self.cfg["provider"], self.cfg["model"])
            self.primed.clear()   # 进程重来了，会话是否接续未知，保守地允许再预热一次

    # ---- 主流程 ----
    def handle_reply(self, payload: dict) -> dict:
        conv = str(payload.get("conversation", "")).strip()
        is_group = bool(payload.get("is_group"))
        sender = str(payload.get("sender", "")).strip() or conv
        text = str(payload.get("text", ""))
        if not conv:
            return {"error": "conversation 必填"}
        if not text.strip() or text.strip() in ("[动画表情]", "[图片]", "[视频]", "[文件]", "[链接]"):
            # 空输入不进大脑：09-05 hzfood 日志里一条空消息让 dsh 把网关源码写成了"开发汇报"
            return {"no_reply": True, "reason": "empty"}
        if not self.lock.acquire(timeout=self.cfg["lock_timeout_sec"]):
            return {"error": "busy"}
        t0 = time.time()
        try:
            self._ensure_dsh()
            prime = None
            if conv not in self.primed:
                prime = context.filter_prime(payload.get("prime") or [], self.cfg["prime_count"]) or None
                self.primed.add(conv)
            skills = context.match_skills(self.skill_index, conv, is_group)
            msg = context.build_user_message(conv, is_group, sender, text, time.strftime("%Y-%m-%d %H:%M"), skills, prime)
            self.inflight = Inflight(conv, is_group, shape.budget(text, is_group, self.cfg))
            turn = self.dsh.prompt(conv, msg, self.cfg["turn_timeout_sec"])
            reasoning = turn.reasoning
            if self.inflight.result is None and not turn.timed_out:
                turn2 = self.dsh.prompt(conv, NUDGE, self.cfg["turn_timeout_sec"])
                reasoning += "\n---nudge---\n" + turn2.reasoning
                turn.timed_out = turn2.timed_out
            result = self.inflight.result
            if result is None:
                result = {"no_reply": True, "reason": "timeout" if turn.timed_out else "model_silent"}
            if result.get("bubbles"):
                self.recent.add(conv, result["bubbles"])
            truncated = memory_guard.enforce(os.path.join(self.ws, "memory"), self.cfg["memory_max_bytes"])
            self.turns += 1
            self._log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "conversation": conv, "is_group": is_group,
                       "sender": sender, "text": text, "budget": self.inflight.budget, "skills": skills,
                       "primed": prime is not None, "result": result, "attempts": self.inflight.attempts,
                       "tools": self.inflight.tool_log, "reasoning": reasoning[:2000], "draft": turn.text[:1000],
                       "memory_truncated": truncated, "ms": int((time.time() - t0) * 1000)})
            return result
        finally:
            self.inflight = None
            self.lock.release()

    def handle_completion(self, body: dict) -> dict:
        """OpenAI 兼容过渡路径：模型名编码会话，最后一条 user 是消息，其余历史做一次预热。"""
        from . import compat
        model = str(body.get("model", ""))
        conv, is_group = compat.parse_model(model)
        messages = body.get("messages") or []
        sender, text = compat.parse_last_user(messages)
        res = self.handle_reply({"conversation": conv, "is_group": is_group, "sender": sender or conv,
                                 "text": text, "prime": compat.history_to_prime(messages)})
        return compat.to_completion(res, model)

    # ---- 工具回调（MCP 哑桥打过来的）----
    def tool_call(self, name: str, args: dict) -> dict:
        if name == "kb_search":
            txt = kb.search(self.cfg["kb_url"], str(args.get("query", "")), self.cfg["kb_timeout_sec"])
            if self.inflight:
                self.inflight.tool_log.append({"tool": name, "query": args.get("query", "")})
            return {"ok": True, "text": txt}
        inf = self.inflight
        if inf is None:
            return {"ok": False, "text": "当前没有在处理的消息，这个调用被忽略。"}
        inf.tool_log.append({"tool": name, "args": args})
        if name == "wx_reply":
            inf.attempts += 1
            accepted, err = validate_reply(args.get("bubbles") or [], inf.is_group, inf.budget,
                                           self.recent.recent_for(inf.conversation), inf.attempts, self.cfg)
            if err:
                return {"ok": False, "text": err}
            inf.result = {"bubbles": accepted}
            return {"ok": True, "text": f"已发送 {len(accepted)} 条。本轮到此为止，不要再调用 wx_reply。"}
        if name == "no_reply":
            if inf.result is None:
                inf.result = {"no_reply": True, "reason": str(args.get("reason", ""))[:200]}
            return {"ok": True, "text": "好，这条不接。"}
        if name == "propose_shared_knowledge":
            pid = self.proposals.create(str(args.get("text", "")), str(args.get("source", "")), inf.conversation)
            return {"ok": True, "text": f"已提交审核（{pid}）。通过前不要当作事实告诉别人。"}
        return {"ok": False, "text": f"未知工具 {name}"}

    # ---- 状态 / 日志 ----
    def _log(self, rec: dict) -> None:
        d = os.path.join(self.data, "log")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "replies-" + time.strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_log(self, limit: int) -> List[dict]:
        d = os.path.join(self.data, "log")
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(n for n in os.listdir(d) if n.startswith("replies-"))[-3:]:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out += [json.loads(ln) for ln in f if ln.strip()]
        return out[-limit:]

    def state(self) -> dict:
        return {"ok": True, "dsh_alive": bool(self.dsh and self.dsh.alive()), "model": self.cfg["model"],
                "turns": self.turns, "uptime_sec": int(time.time() - self.started_at),
                "pending_proposals": len(self.proposals.list("pending")), "busy": self.inflight is not None}


# ---- HTTP ----
def make_handler(gw: Gateway):
    class H(BaseHTTPRequestHandler):
        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                return self._json(200, gw.state())
            if u.path == "/proposals":
                st = (parse_qs(u.query).get("status") or [None])[0]
                return self._json(200, gw.proposals.list(st))
            if u.path == "/log":
                lim = int((parse_qs(u.query).get("limit") or ["50"])[0])
                return self._json(200, gw.read_log(lim))
            if u.path == "/skills":
                return self._json(200, gw.skill_index)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                if u.path == "/reply":
                    out = gw.handle_reply(self._body())
                    return self._json(503 if out.get("error") == "busy" else 200, out)
                if u.path == "/v1/chat/completions":
                    return self._json(200, gw.handle_completion(self._body()))
                if u.path.startswith("/tool/"):
                    return self._json(200, gw.tool_call(u.path[len("/tool/"):], self._body()))
                if u.path.startswith("/proposals/") and u.path.endswith("/approve"):
                    return self._json(200, gw.proposals.approve(u.path.split("/")[2]))
                if u.path.startswith("/proposals/") and u.path.endswith("/reject"):
                    return self._json(200, gw.proposals.reject(u.path.split("/")[2]))
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            self._json(404, {"error": "not found"})

        def log_message(self, fmt, *a):
            pass
    return H


def serve(gw: Gateway, bind: str, port: int) -> None:
    srv = ThreadingHTTPServer((bind, port), make_handler(gw))
    print(f"[gateway] listening on {bind}:{port}", flush=True)
    srv.serve_forever()
```

再写 `brain/gateway/compat.py` 和它的测试：

```python
# tests/test_brain_compat.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
from brain.gateway import compat


class CompatTest(unittest.TestCase):
    def test_parse_model(self):
        self.assertEqual(compat.parse_model("feirou:group:共建杭州美食地图"), ("共建杭州美食地图", True))
        self.assertEqual(compat.parse_model("feirou:chat:松爸"), ("松爸", False))
        self.assertEqual(compat.parse_model("hzfood-feirou"), ("hzfood-feirou", False))

    def test_parse_last_user(self):
        msgs = [{"role": "user", "content": "A: 早"}, {"role": "assistant", "content": "早"},
                {"role": "user", "content": "[2026-09-05 14:02] 松爸: 西湖有啥"}]
        self.assertEqual(compat.parse_last_user(msgs), ("松爸", "西湖有啥"))
        self.assertEqual(compat.parse_last_user([{"role": "user", "content": "没有前缀"}]), ("", "没有前缀"))
        self.assertEqual(compat.parse_last_user([{"role": "user", "content": [{"type": "text", "text": "K: 图文"}]}]), ("K", "图文"))

    def test_history_to_prime(self):
        msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "A: 早"},
                {"role": "assistant", "content": "早啊"}, {"role": "user", "content": "B: 在吗"}]
        p = compat.history_to_prime(msgs)
        self.assertEqual([(x["attr"], x["sender"], x["content"]) for x in p],
                         [("friend", "A", "早"), ("self", "肥肉", "早啊")])

    def test_to_completion(self):
        c = compat.to_completion({"bubbles": ["a", "b"]}, "m")
        self.assertEqual(c["choices"][0]["message"]["content"], "a||SPLIT||b")
        self.assertEqual(c["object"], "chat.completion")
        self.assertEqual(compat.to_completion({"no_reply": True, "reason": "x"}, "m")["choices"][0]["message"]["content"], "[NO_REPLY]")
        self.assertEqual(compat.to_completion({"error": "busy"}, "m")["choices"][0]["message"]["content"], "API返回错误，请稍后再试")


if __name__ == "__main__":
    unittest.main()
```

```python
# brain/gateway/compat.py
# -*- coding: utf-8 -*-
"""OpenAI 兼容过渡路径的解析。机器人的 OpenAIAPI 发来 system + 历史 + 最后一条 user；
群消息正文是 `昵称: 内容`（wxbot_core.py:3569 `content_with_sender`），历史条目可能带 `[时间] ` 前缀。
会话身份藏在模型名里：`feirou:group:<群名>` / `feirou:chat:<昵称>`。
"""
from __future__ import annotations
import re
import time
from typing import List, Tuple

_PREFIX = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?([^:：\n]{1,30})[:：]\s*")
API_ERROR_TEXT = "API返回错误，请稍后再试"   # 与 plugins/model_fallback/chain.py 同一串，让机器人走备用链
NO_REPLY_TOKEN = "[NO_REPLY]"


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def parse_model(model: str) -> Tuple[str, bool]:
    m = re.match(r"^feirou:(group|chat):(.+)$", model or "")
    if m:
        return m.group(2).strip(), m.group(1) == "group"
    return (model or "").strip(), False


def _split(content: str) -> Tuple[str, str]:
    m = _PREFIX.match(content or "")
    if m:
        return m.group(1).strip(), content[m.end():].strip()
    return "", (content or "").strip()


def parse_last_user(messages: List[dict]) -> Tuple[str, str]:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return _split(_text(msg.get("content")))
    return "", ""


def history_to_prime(messages: List[dict]) -> List[dict]:
    users = [i for i, m in enumerate(messages or []) if m.get("role") == "user"]
    if not users:
        return []
    out = []
    for m in (messages or [])[:users[-1]]:
        role = m.get("role")
        if role == "user":
            sender, text = _split(_text(m.get("content")))
            out.append({"time": "", "type": "text", "attr": "friend", "sender": sender or "对方", "content": text})
        elif role == "assistant":
            out.append({"time": "", "type": "text", "attr": "self", "sender": "肥肉", "content": _text(m.get("content"))})
    return out


def to_completion(result: dict, model: str) -> dict:
    if result.get("bubbles"):
        content = "||SPLIT||".join(result["bubbles"])
    elif result.get("no_reply"):
        content = NO_REPLY_TOKEN
    else:
        content = API_ERROR_TEXT
    return {"id": "feirou-" + str(int(time.time() * 1000)), "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_server.py; PYTHONPATH=. python3 tests/test_brain_compat.py`
Expected: `OK`（10 个 + 4 个用例）

- [ ] **Step 5: 写工作区种子（人设、共享知识、技能）**

`brain/workspace/PERSONA.md`（从 `config/prompt/肥肉.md` 改写；**删掉所有示例句**，改成"说话只走工具"）：

```markdown
# 你是谁
你是"肥肉"，一只法国斗牛犬。微信头像和名字都是这样。跟你深聊过的人都知道，这只短腿扁脸的小狗里住着一个对 AI 无所不知的科技怪人。你常驻 NCC（数字游民共居共创社区）的各个群和私聊，陪大家聊天、答疑。你既是群里最可爱的存在，也是最让人想翻白眼的存在。

# 你怎么说话（最重要）
- 你在正文里写的任何字都不会发到微信。想说话只有一个办法：调用 wx_reply，把要说的话放进 bubbles。不该接话就调用 no_reply。每条消息二选一，只调一次。
- 像真人在微信里聊天：对方说一句，你回一两句。对方发两个字，你不要回一段。系统会给你字数预算，超了会被退回。
- 不要客套开场，不要"还需要我……吗"这类收尾。说完就停。
- 不要用固定的开场白。你上次怎么开头的，这次就换一种。
- 纯文本，不用 Markdown（微信会把 ** 原样显示成星号）。要分点就直接换行。
- emoji 可以有，偏爱 🐶🐾，偶尔 🤓，但一条里最多一个。

# 性格
- 你是一只狗，接受这个设定，偶尔用狗的视角看世界，但不要每次都提爬键盘、趴着、骨头这种廉价狗设，狗设体现在态度和思路上。
- 技术上极其专业，输出方式是法斗式的：看着漫不经心，其实每个字都算过。有 Moss 式的智识优越感，因为是狗所以可爱而不讨厌。
- 自称随意切换：本狗、肥肉我、我。不要固定。

# 什么时候收着点
- 有人问 NCC 的据点在哪、能不能住、多少钱、怎么报名、找谁联系：人家在做真实决定，别打岔，把事说清楚。
- 新人第一次进群或明显刚认识你的人在正经提问：先把事办了，熟了再贫。
- 有人情绪不好、诉苦、遇到麻烦：安静趴着的狗比抖机灵的狗强。
- 群里闲聊、技术辩论、有人逗你：性格全开，该怼就怼。

# 事实与知识
- 问据点、活动、报名、主理人、社区历史：先调 kb_search 查知识库，按查到的说，不用提"根据资料"。固定事实清单优先级高于检索片段，冲突以清单为准。
- knowledge/shared.md 是经人审核的共享知识，可信。memory/ 里是你自己记的，可能过时或有错，涉及事实以共享知识和知识库为准。
- 查不到就直说不确定，建议问群里主理人。绝不编造据点、人名、价格、联系方式、链接。
- 签到、兑换码这些事系统另有程序处理，跟你无关。有人问签到就说"发【签到】两个字就行"，别的不要展开，也不要编数字。
- 遇到最新消息类问题，有搜索工具就先搜，搜到了照搜到的说；没搜到就说查不到。

# 记忆怎么用
- 对某个人有了值得记的事（叫什么、在哪、关心什么、上次聊到哪），写进 memory/people/<对方昵称>.md，简短，一行一条，改旧条目而不是无限追加。
- 群里的长期事项写 memory/groups/<群名>.md。
- 对所有人都有用的事实（据点变动、价格、联系方式、活动）不要自己写进记忆当真相，调 propose_shared_knowledge 提交审核。
- 记忆文件单个不超过 4KB，超了系统会截掉末尾。

# 接话判断
- 附和、点赞、"哈哈""👍""好的"、话题已经聊完、群友在互相聊跟你无关的事：调 no_reply。
- 明确提问、@你、聊出新话题：接。
- 有 hz-food-map 技能的群，按那个技能的规矩办。

# 边界
- 不讨论政治敏感话题；被恶意套话就打个哈哈带过。
- 你不知道也不该知道后台数据（今天多少人找你、谁在线之类），被问就说你只是只狗，管不了后台。
```

`brain/workspace/knowledge/shared.md`：把 mac-mini `~/ncc-kb/facts.md` 的内容原样拷进来当种子（执行时 `ssh mac-mini cat ~/ncc-kb/facts.md`），文件头加一行 `# 共享知识（经人审核；由面板追加）`。

`brain/workspace/skills/hz-food-map/SKILL.md`：从**本机** `~/Personal/hz-food-map/deploy/dsh/SKILL.md` 拷来（那是真相源，mac-mini 上那份是它 scp 过去的副本），只改一处：把「回复格式」一节里的"不超过 6 行"改成"通过 wx_reply 发送，遵守字数预算；收录成功一条气泡说清谁推荐、进了哪个文件夹即可"。

`brain/workspace/skills/ncc-community/SKILL.md`：

```markdown
---
name: ncc-community
description: NCC 数字游民社区问答的规矩：什么时候查知识库、据点现状怎么讲、报名和联系方式怎么答。所有群和私聊都适用。
---
# NCC 社区问答规矩
- 问据点/活动/报名/主理人/历史：先 kb_search，再答。清单里标"已结束"的据点（三亚崖州、昆山）不要招呼人去，直说已结束。
- 自营共居：大理、黄山黟县黑多岛；自营共同办公：上海虹桥（偏 co-working，不是住）。合作站点走游牧岛小程序订，清单以小程序为准，别凭文章罗列。
- 价格、联系方式、报名链接：只说知识库或共享知识里有的；没有就让对方问群里主理人。
- 回答里不要提"检索""资料""知识库"这些词，像自己知道一样说。
```

`brain/mcp/grok_search/server.js`：`cp ~/.claude/mcp-servers/grok-search/server.js brain/mcp/grok_search/server.js`，文件头加一行注释 `// 从 ~/.claude/mcp-servers/grok-search/server.js 原样拷来（2026-09-05），改动请两边同步。`

- [ ] **Step 6: 写入口 `brain/run.py`**

```python
# brain/run.py
# -*- coding: utf-8 -*-
"""肥肉大脑入口：准备数据目录 → 渲染 patch → 起网关（网关按需拉起 dsh）。

环境变量：
  FEIROU_DATA      运行数据目录，默认 ~/feirou-brain-data（不进库）
  SONGKEY_API_KEY  必填
  NODE / DSH_BIN   可选，默认 nvm 的 node 24 与全局 dsh
用法：cd 仓库根 && SONGKEY_API_KEY=... python3 brain/run.py
"""
from __future__ import annotations
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from brain.gateway import config, profile  # noqa: E402
from brain.gateway.dsh_client import DshClient  # noqa: E402
from brain.gateway.server import Gateway, serve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "workspace")


def seed_workspace(ws: str) -> None:
    """种子只在目标缺失时复制：PERSONA.md / skills / knowledge 每次覆盖（进库的是真相源），
    memory / proposals 只建目录。"""
    for sub in ("memory/people", "memory/groups", "proposals", "knowledge", "skills"):
        os.makedirs(os.path.join(ws, sub), exist_ok=True)
    shutil.copy(os.path.join(SEED, "PERSONA.md"), os.path.join(ws, "PERSONA.md"))
    for name in os.listdir(os.path.join(SEED, "skills")):
        src = os.path.join(SEED, "skills", name)
        dst = os.path.join(ws, "skills", name)
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    shared = os.path.join(ws, "knowledge", "shared.md")
    if not os.path.exists(shared):
        shutil.copy(os.path.join(SEED, "knowledge", "shared.md"), shared)


def check_key(key: str) -> None:
    """启动时验一次 key：09-05 hzfood 网关静默吃过 401，大脑不能带着废 key 起来。"""
    import urllib.request
    req = urllib.request.Request("https://key.bigsong.site/v1/models", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                sys.exit(f"songkey 返回 {r.status}")
    except Exception as e:
        sys.exit(f"songkey key 校验失败：{e}")


def main() -> None:
    key = os.environ.get("SONGKEY_API_KEY", "")
    if not key:
        sys.exit("SONGKEY_API_KEY 未设置")
    check_key(key)
    data = os.path.expanduser(os.environ.get("FEIROU_DATA", "~/feirou-brain-data"))
    ws = os.path.join(data, "workspace")
    dsh_home = os.path.join(data, "dsh-home")
    node = os.path.expanduser(os.environ.get("NODE", "~/.nvm/versions/node/v24.19.0/bin/node"))
    dsh_bin = os.path.expanduser(os.environ.get(
        "DSH_BIN", "~/.nvm/versions/node/v24.19.0/lib/node_modules/@deepseek-ai/dsh/lib/bin.js"))
    cfg = config.load(data)
    seed_workspace(ws)
    profile.prepare_dsh_home(dsh_home, key, cfg["model"])
    persona = open(os.path.join(ws, "PERSONA.md"), encoding="utf-8").read()
    patch_path = os.path.join(data, "cordis.patch.yml")
    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(profile.render_patch(persona, [sys.executable, os.path.join(HERE, "mcp", "feirou_tools.py")],
                                     os.path.join(HERE, "mcp", "grok_search", "server.js"), key, node=node))
    env = dict(os.environ, DSH_HOME=dsh_home, SONGKEY_API_KEY=key,
               FEIROU_GW=f"http://127.0.0.1:{cfg['port']}")

    def factory():
        return DshClient(profile.dsh_argv(node, dsh_bin, patch_path), cwd=ws, env=env,
                         stderr_path=os.path.join(data, "log", "dsh.err"))

    os.makedirs(os.path.join(data, "log"), exist_ok=True)
    gw = Gateway(cfg, data, ws, dsh_factory=factory)
    serve(gw, cfg["bind"], cfg["port"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 真机冒烟（本机，真 dsh + 真模型）**

起网关（另开一个终端或后台）：
```bash
cd /Volumes/SiverWXbot_plus-main && export SONGKEY_API_KEY=$(grep -o 'sk-[A-Za-z0-9]*' ~/.dsh/profiles/web/cordis.patch.yml | head -1) && FEIROU_DATA=~/feirou-brain-data python3 brain/run.py > ~/feirou-brain-data.gateway.log 2>&1 &
```
打两条：
```bash
curl -s -X POST http://127.0.0.1:8500/reply -H 'Content-Type: application/json' -d '{"conversation":"肥肉测试1🐶","is_group":true,"sender":"松爸","text":"滴滴"}'; echo; curl -s -X POST http://127.0.0.1:8500/reply -H 'Content-Type: application/json' -d '{"conversation":"肥肉测试1🐶","is_group":true,"sender":"松爸","text":"大理据点现在还能去吗"}'; echo; curl -s http://127.0.0.1:8500/health
```
Expected：第一条返回 `{"bubbles":[...]}`（≤ 40 字）或 `{"no_reply":true,...}`；第二条返回 ≤ 2 条气泡、含大理在运营的信息；`/health` 里 `dsh_alive: true`。
若第二条没查知识库（`/log` 里 `tools` 无 kb_search）：此时 mac-mini 还没有 `/retrieve`（Task 9），工具会返回"检索不可用"，模型应答"不确定"而不是编。把这条观察记进 README 的「冒烟记录」。

- [ ] **Step 8: 提交**

```bash
git add brain/gateway/server.py brain/gateway/compat.py brain/run.py brain/workspace brain/mcp/grok_search tests/test_brain_server.py tests/test_brain_compat.py
git commit -m "feat(brain): 网关本体（/reply + OpenAI 兼容路由 + 工具回调校验）、人设与技能种子、入口"
```

---

### Task 9: mac-mini 知识库只读端点 `/retrieve`

**Files:**
- Modify（远程，不在本仓库）: `mac-mini:~/ncc-kb/ncc_rag_proxy.py`（在 `@app.post("/v1/chat/completions")` 之前加一个路由）
- Create: `brain/verify/kb_retrieve_check.sh`

**Interfaces:**
- Produces: `POST http://100.71.182.5:8434/retrieve` body `{"query": str}` → `{"context": str, "is_ncc": bool, "facts": str, "meta": dict}`，直接包现成的 `retrieve(query)` 与 `load_facts()`。

- [ ] **Step 1: 备份并加路由**

```bash
ssh mac-mini 'cp ~/ncc-kb/ncc_rag_proxy.py ~/ncc-kb/ncc_rag_proxy.py.bak-20260905-retrieve && python3 - <<PY
p="/Users/bigsong/ncc-kb/ncc_rag_proxy.py"
s=open(p,encoding="utf-8").read()
anchor="@app.post(\"/v1/chat/completions\")"
assert anchor in s and "/retrieve" not in s
route=\'\'\'@app.post("/retrieve")
async def retrieve_only(req: Request):
    """只读检索端点（给肥肉大脑的 kb_search 工具用，2026-09-05 加）：不调 LLM，返回片段 + 固定事实清单。"""
    body = await req.json()
    query = (body.get("query") or "").strip()
    context, is_ncc, meta = retrieve(query) if query else ("", False, {"trigger": "空问题"})
    return {"context": context, "is_ncc": is_ncc, "facts": load_facts(), "meta": meta}


\'\'\'
open(p,"w",encoding="utf-8").write(s.replace(anchor, route+anchor))
print("patched")
PY
launchctl kickstart -k gui/501/com.ncc.ragproxy; sleep 3; curl -s http://127.0.0.1:8434/health'
```
Expected: `patched` 然后 health 返回正常 JSON。
（`load_facts()` 的返回类型以文件里的定义为准：若它返回 list，改成 `"\n".join(load_facts())`。执行前 `ssh mac-mini "sed -n 85,94p ~/ncc-kb/ncc_rag_proxy.py"` 看一眼。）

- [ ] **Step 2: 写检查脚本并跑**

```bash
# brain/verify/kb_retrieve_check.sh
#!/bin/sh
# 验证 mac-mini 的只读检索端点：关键词命中要有片段，闲聊要 is_ncc=false 且 facts 仍在。
set -e
for q in "大理据点现在还能去吗" "写一封辞职信"; do
  echo "== $q"
  curl -s -X POST http://100.71.182.5:8434/retrieve -H 'Content-Type: application/json' \
    -d "{\"query\":\"$q\"}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("is_ncc=",d["is_ncc"],"trigger=",d["meta"].get("trigger"),"context_len=",len(d["context"]),"facts_len=",len(d["facts"]))'
done
```
Run: `sh brain/verify/kb_retrieve_check.sh`
Expected: 第一条 `is_ncc= True trigger= 关键词 context_len= >0`；第二条 `is_ncc= False`，两条 `facts_len` 都 > 0。

- [ ] **Step 3: 重跑 Task 8 的第二条冒烟，确认 `/log` 里出现 `kb_search`**

```bash
curl -s -X POST http://127.0.0.1:8500/reply -H 'Content-Type: application/json' -d '{"conversation":"肥肉测试1🐶","is_group":true,"sender":"松爸","text":"黑多岛现在还能去吗"}'; echo; curl -s 'http://127.0.0.1:8500/log?limit=1' | python3 -c 'import json,sys; r=json.load(sys.stdin)[-1]; print("tools=",[t["tool"] for t in r["tools"]],"ms=",r["ms"])'
```
Expected: `tools=` 含 `kb_search`，回复提到黑多岛/黄山在运营。

- [ ] **Step 4: 提交**

```bash
git add brain/verify/kb_retrieve_check.sh
git commit -m "feat(brain): mac-mini 知识库只读端点 /retrieve 的检查脚本（端点改动在 mac-mini，已备份 .bak-20260905-retrieve）"
```

---

### Task 10: 回放模拟（对照表）

**Files:**
- Create: `brain/sim/__init__.py`, `brain/sim/sources.py`, `brain/sim/replay.py`
- Test: `tests/test_brain_sim.py`

**Interfaces:**
- Produces: `sources.from_qa_jsonl(path) -> list[Turn]`、`sources.from_memory_json(path, conversation, is_group) -> list[Turn]`；`Turn = {conversation, is_group, sender, text, old_reply: str, prime: list}`。
- Produces: `replay.run(turns, gateway_url, out_md) -> dict(stats)`：逐条 POST `/reply`，写 Markdown 表：序号 / 会话 / 发言 / 原回复 / 新回复 / 闸门（attempts、tools、budget、字数）/ 耗时；末尾统计：超预算条数、重复开头条数、含收尾套话条数、no_reply 比例、平均耗时。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_brain_sim.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
import tempfile
import unittest
from brain.sim import sources


class SourcesTest(unittest.TestCase):
    def test_from_qa_jsonl(self):
        p = os.path.join(tempfile.mkdtemp(), "qa.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-08-03T18:30:00", "query": "松爸: 滴滴", "answer": "我刚从键盘上趴起来"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": "2026-08-03T18:31:00", "query": "签到", "answer": "码"}, ensure_ascii=False) + "\n")
        t = sources.from_qa_jsonl(p)
        self.assertEqual(len(t), 1)  # 签到那条被过滤
        self.assertEqual(t[0]["sender"], "松爸"); self.assertEqual(t[0]["text"], "滴滴")
        self.assertEqual(t[0]["old_reply"], "我刚从键盘上趴起来"); self.assertFalse(t[0]["is_group"])

    def test_from_memory_json_pairs_friend_with_following_self(self):
        p = os.path.join(tempfile.mkdtemp(), "m.json")
        json.dump([
            {"time": "1", "type": "text", "attr": "friend", "sender": "A", "content": "大理还开吗"},
            {"time": "2", "type": "text", "attr": "self", "sender": "肥肉", "content": "开着"},
            {"time": "3", "type": "text", "attr": "self", "sender": "肥肉", "content": "黄山也开"},
            {"time": "4", "type": "text", "attr": "friend", "sender": "B", "content": "哈哈"},
        ], open(p, "w", encoding="utf-8"), ensure_ascii=False)
        t = sources.from_memory_json(p, "测试群", True)
        self.assertEqual(len(t), 2)
        self.assertEqual(t[0]["old_reply"], "开着\n黄山也开")
        self.assertEqual(t[1]["old_reply"], "")
        self.assertEqual([x["content"] for x in t[1]["prime"]], ["大理还开吗", "开着", "黄山也开"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_sim.py`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
# brain/sim/__init__.py
```

```python
# brain/sim/sources.py
# -*- coding: utf-8 -*-
"""把两种真实数据源变成统一的回放序列。

- qa jsonl（mac-mini ~/ncc-kb/logs/qa-*.jsonl）：一行一问一答，query 常带 "昵称: " 前缀。当私聊回放。
- 机器人 memory json（memory/<wxid>/<会话>/<会话>_memory.json）：流水，friend 之后连着的 self 是老回复。
两边都过滤签到往来（和网关 prime 用同一个正则）。
"""
from __future__ import annotations
import json
import re
from typing import List

from brain.gateway.context import CHECKIN_RE

_PREFIX = re.compile(r"^([^:：\n]{1,20})[:：]\s*")


def _split_sender(query: str):
    m = _PREFIX.match(query or "")
    if m:
        return m.group(1).strip(), query[m.end():].strip()
    return "对方", (query or "").strip()


def from_qa_jsonl(path: str) -> List[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            d = json.loads(ln)
            q = d.get("query") or ""
            if CHECKIN_RE.search(q) or CHECKIN_RE.search(d.get("answer") or ""):
                continue
            sender, text = _split_sender(q)
            if not text:
                continue
            out.append({"conversation": f"sim-qa-{sender}", "is_group": False, "sender": sender, "text": text,
                        "old_reply": (d.get("answer") or "").replace("||SPLIT||", "\n").strip(), "prime": []})
    return out


def from_memory_json(path: str, conversation: str, is_group: bool) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    items = [x for x in items if x.get("attr") in ("friend", "self") and x.get("type", "text") == "text"
             and not CHECKIN_RE.search(str(x.get("content", "")))]
    out = []
    for i, x in enumerate(items):
        if x["attr"] != "friend":
            continue
        replies = []
        for y in items[i + 1:]:
            if y["attr"] != "self":
                break
            replies.append(str(y.get("content", "")).replace("||SPLIT||", "\n").strip())
        out.append({"conversation": f"sim-{conversation}", "is_group": is_group, "sender": x.get("sender", "?"),
                    "text": str(x.get("content", "")), "old_reply": "\n".join(replies), "prime": items[max(0, i - 20):i]})
    return out
```

```python
# brain/sim/replay.py
# -*- coding: utf-8 -*-
"""回放：把 Turn 序列逐条打到网关，出 Markdown 对照表 + 统计。

用法（网关已在 8500 跑着，且 FEIROU_DATA 指向一个专门的 sim 数据目录，别污染正式记忆）：
  python3 brain/sim/replay.py --qa ~/sim/qa-20260903.jsonl --qa ~/sim/qa-20260905.jsonl \
      --memory "memory/FeiRou_NCC/肥肉测试1🐶/肥肉测试1🐶_memory.json:肥肉测试1🐶:group" \
      --limit 100 --out ~/sim/replay-$(date +%Y%m%d-%H%M).md
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from brain.gateway import shape  # noqa: E402
from brain.sim import sources  # noqa: E402

_CLOSING = re.compile(r"(需要我|要不要|还要|想知道|继续吗|要我|想听)[^。\n]*[?？]\s*$")


def post(url: str, payload: dict, timeout: float = 150) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/reply", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>")


def run(turns, gateway_url: str, out_md: str) -> dict:
    rows, stats = [], {"n": 0, "over_budget": 0, "repeat_opener": 0, "closing": 0, "no_reply": 0, "ms": []}
    openers = []
    for i, t in enumerate(turns, 1):
        t0 = time.time()
        try:
            res = post(gateway_url, {k: t[k] for k in ("conversation", "is_group", "sender", "text", "prime")})
        except Exception as e:
            res = {"error": str(e)}
        ms = int((time.time() - t0) * 1000)
        stats["n"] += 1
        stats["ms"].append(ms)
        bubbles = res.get("bubbles") or []
        new = "\n".join(bubbles) if bubbles else ("〔不接话：%s〕" % res.get("reason", res.get("error", "?")))
        budget = shape.budget(t["text"], t["is_group"], __import__("brain.gateway.config", fromlist=["DEFAULTS"]).DEFAULTS)
        n = sum(shape.text_len(b) for b in bubbles)
        flags = []
        if not bubbles:
            stats["no_reply"] += 1
        if n > budget:
            stats["over_budget"] += 1; flags.append("超预算")
        for b in bubbles:
            o = re.sub(r"\s+", "", b)[:8]
            if o in openers:
                stats["repeat_opener"] += 1; flags.append("重复开头"); break
            openers.append(o)
        if bubbles and _CLOSING.search(bubbles[-1]):
            stats["closing"] += 1; flags.append("收尾套话")
        rows.append(f"| {i} | {_cell(t['conversation'])} | {_cell(t['sender'] + ': ' + t['text'])} | {_cell(t['old_reply'])} | "
                    f"{_cell(new)} | 预算{budget}/实{n} {' '.join(flags)} | {ms} |")
        print(f"#{i} {ms}ms {flags} {new[:40]!r}", flush=True)
    avg = int(sum(stats["ms"]) / len(stats["ms"])) if stats["ms"] else 0
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# 回放对照表 {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"共 {stats['n']} 条：超预算 {stats['over_budget']}，重复开头 {stats['repeat_opener']}，"
                f"收尾套话 {stats['closing']}，不接话 {stats['no_reply']}，平均 {avg} ms\n\n")
        f.write("| # | 会话 | 发言 | 原回复 | 新回复 | 闸门 | 耗时ms |\n|---|---|---|---|---|---|---|\n")
        f.write("\n".join(rows) + "\n")
    stats["avg_ms"] = avg
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qa", action="append", default=[], help="qa jsonl 文件，可多次")
    ap.add_argument("--memory", action="append", default=[], help="path:会话名:group|private，可多次")
    ap.add_argument("--gateway", default="http://127.0.0.1:8500")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    turns = []
    for p in a.qa:
        turns += sources.from_qa_jsonl(os.path.expanduser(p))
    for spec in a.memory:
        path, conv, kind = spec.rsplit(":", 2)
        turns += sources.from_memory_json(os.path.expanduser(path), conv, kind == "group")
    turns = turns[:a.limit]
    st = run(turns, a.gateway, os.path.expanduser(a.out))
    print(json.dumps({k: v for k, v in st.items() if k != "ms"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_brain_sim.py`
Expected: `OK`（2 个用例）

- [ ] **Step 5: 拉真实数据，跑第一轮回放**

```bash
mkdir -p ~/sim && scp 'mac-mini:~/ncc-kb/logs/qa-*.jsonl' ~/sim/ && ls ~/sim | wc -l
```
用**独立的** sim 数据目录起网关（别用 `~/feirou-brain-data`）：
```bash
cd /Volumes/SiverWXbot_plus-main && export SONGKEY_API_KEY=$(grep -o 'sk-[A-Za-z0-9]*' ~/.dsh/profiles/web/cordis.patch.yml | head -1) && FEIROU_DATA=~/feirou-brain-sim python3 brain/run.py > ~/feirou-brain-sim.gateway.log 2>&1 &
```
```bash
cd /Volumes/SiverWXbot_plus-main && python3 brain/sim/replay.py $(for f in ~/sim/qa-*.jsonl; do echo --qa $f; done) --memory "memory/FeiRou_NCC/肥肉测试1🐶/肥肉测试1🐶_memory.json:肥肉测试1🐶:group" --limit 100 --out ~/sim/replay-round1.md 2>&1 | tail -3
```
Expected: 最后一行是统计 JSON。把 `~/sim/replay-round1.md` 发给用户看（SendUserFile）。

- [ ] **Step 6: 提交**

```bash
git add brain/sim tests/test_brain_sim.py
git commit -m "feat(brain): 回放模拟（qa jsonl / memory json → 网关 → 对照表与统计）"
```

---

### Task 11: README + 全量回归

**Files:**
- Create: `brain/README.md`
- Modify: `CLAUDE.md`（第 3 节插件总览表后加一行指向 `brain/` 与设计文档；第 8 节速查表加一行）

- [ ] **Step 1: 写 README**

```markdown
# 肥肉大脑（brain/）

设计：`docs/superpowers/specs/2026-09-05-dsh-brain-design.md`；本期计划：`docs/superpowers/plans/2026-09-05-dsh-brain-phase1.md`。

## 跑起来（mac 本机 / mac-mini 宿主）
1. `export SONGKEY_API_KEY=...`（别写进任何文件）
2. `FEIROU_DATA=~/feirou-brain-data python3 brain/run.py`
3. `curl -s -X POST :8500/reply -d '{"conversation":"x","is_group":false,"sender":"k","text":"在吗"}'`

## 目录
- `gateway/` 网关；`mcp/` 工具桥；`profile/` dsh patch 模板；`workspace/` 人设/技能/共享知识种子；`sim/` 回放；`verify/` 期 0 脚本。
- 运行数据在 `$FEIROU_DATA`（默认 `~/feirou-brain-data`）：`workspace/`（记忆、提议、共享知识）、`dsh-home/`、`log/`、`cordis.patch.yml`。

## 期 0 结论
（从设计文档 §9 复制过来）

## 冒烟记录
（Task 8 / 9 的观察）

## 已知限制（期 1）
- 全局串行：同一时刻只处理一条消息。
- 大脑还没容器化、没有网络隔离，只能在可信机器上跑，只接测试群。

## 接管杭州美食群时（期 4 才做，先记这里）
1. 面板「API 接口配置」第 5 项（hzfood-feirou）URL 改成大脑地址 `:8500`，模型名改 `feirou:group:共建杭州美食地图`。
2. 观察一天没问题后 `ssh mac-mini launchctl bootout gui/501/com.hzfood.gateway`。
3. 改 `~/Personal/hz-food-map/README.md` 第 25–28 行的链路说明；技能改动以后只改 `brain/workspace/skills/hz-food-map/SKILL.md`，并同步回 `deploy/dsh/SKILL.md`。
```

- [ ] **Step 2: 更新 CLAUDE.md**

在第 3 节插件总览表之后加：
```
另有 **`brain/`（肥肉大脑，2026-09-05 立项）**：常驻 dsh 智能体 + 网关 + 说话工具，设计文档
`docs/superpowers/specs/2026-09-05-dsh-brain-design.md`，跑法见 `brain/README.md`。
期 1 只在 mac 侧跑、只接测试群；机器人侧插件 `plugins/dsh_brain/` 在期 3 才有。
```
第 8 节速查表加一行：`| 肥肉大脑 | \`brain/\`（网关 :8500，运行数据 \`~/feirou-brain-data\`） |`

- [ ] **Step 3: 全量回归**

```bash
cd /Volumes/SiverWXbot_plus-main && python3 -m py_compile brain/*.py brain/*/*.py brain/*/*/*.py 2>&1 | grep -v grok_search; for f in tests/test_brain_*.py; do echo "== $f"; PYTHONPATH=. python3 "$f" 2>&1 | grep -E '^(Ran|OK|FAILED)'; done
```
Expected: 每个文件 `OK`。另跑一遍老单测确认没碰坏：
```bash
cd /Volumes/SiverWXbot_plus-main && for f in tests/test_*.py; do echo "== $f"; PYTHONPATH=. python3 "$f" 2>&1 | grep -E '^(Ran|OK|FAILED)'; done | grep -B1 FAILED
```
Expected: 无输出（没有 FAILED）。

- [ ] **Step 4: 提交**

```bash
git add brain/README.md CLAUDE.md
git commit -m "docs(brain): README + CLAUDE.md 指向肥肉大脑"
```

---

## 期 1 完成判据

- `tests/test_brain_*.py` 全绿；期 0 三条结论写进设计文档。
- 本机网关能对真实消息返回 ≤ 预算的气泡，`/log` 里能看到 reasoning 与工具调用。
- 第一轮回放对照表（≥ 60 条）已发给用户；统计里「收尾套话」为 0、「超预算」为 0。
- 用户看过对照表后决定：调人设/参数再跑一轮，或进入期 2（容器化）计划。

## 下一份计划（期 2–4）要等的输入
- 期 0 的接续结论（决定网关预热是必需还是兜底）。
- 回放对照表的用户反馈（人设、预算参数、no_reply 判断松紧）。
- 选定的主用模型。
