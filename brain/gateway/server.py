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
                    try:
                        return self._json(200, gw.proposals.approve(u.path.split("/")[2]))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                if u.path.startswith("/proposals/") and u.path.endswith("/reject"):
                    try:
                        return self._json(200, gw.proposals.reject(u.path.split("/")[2]))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
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
