# -*- coding: utf-8 -*-
"""大脑网关：一次 /reply = 一轮 dsh 对话；模型只能通过工具回调说话。

并发模型（2026-09-08 起，设计见 docs/superpowers/specs/2026-09-08-concurrency-design.md）：
**同一会话串行、不同会话并行**，总并发由 cfg["max_concurrent"] 封顶（默认 3）。
工具回调按消息里盖的 turn_id 归属到 self.inflight[turn_id]；没带 turn_id 时只有在飞恰好
一轮才回落到它，在飞 ≥2 轮一律拒绝（并发下无法归属，放行就是串台的正门）。
dsh 一个进程跑多个 session 已实测可行（brain/verify/concurrent_sessions.py，
两轮干活区间重叠 6.8 秒、零串台、turn_id 零漏带）。
把 max_concurrent 设成 1 即退回改造前的全局串行行为。
"""
from __future__ import annotations
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from . import context, kb, memory_guard, shape
from .proposals import Proposals
from .replies import RecentReplies
from .validate import validate_reply

# 回放 A/B 里 grok-4.6 会把回复写在正文里、然后把这条追问本身当成"系统提示不该回"而调 no_reply，
# 所以措辞要点明"正文没发出去"，抬头也别用方括号（用户消息首行就是方括号，弱模型分不清）。
NUDGE = ("【网关提示】刚才你写在正文里的内容没有发给对方（正文永远不会发送）。现在只做一件事："
         "要说话就调用 wx_reply，把要说的话精简后放进 bubbles；确实不该接话才调用 no_reply。"
         "本提示不是对方发的消息，不要回复它、不要向对方复述它。")
STALE_TURN = "这个调用属于已经结束的上一轮，已忽略。"
# 并发下同时有多轮在飞，调用又没带 turn_id —— 无从归属，只能让模型重来一次（带上 turn_id）
AMBIGUOUS_TURN = ("这个调用没带 turn_id，而现在同时有多条消息在处理，无法确定它属于哪一条，已忽略。"
                  "请重新调用一次，并带上你这轮消息开头 [ … | 轮次:xxx] 里的那个 turn_id。")
TURN_TOOLS = ("wx_reply", "no_reply", "propose_shared_knowledge")


def _stamp_turn_id(msg: str, turn_id: str) -> str:
    """把轮次标识塞进 build_user_message 产出的首行 `[...]` 收尾处（不改 context.py）。"""
    head, sep, rest = msg.partition("\n")
    if head.endswith("]"):
        head = head[:-1] + f" | 轮次:{turn_id}]"
    return head + sep + rest


@dataclass
class Inflight:
    conversation: str
    is_group: bool
    budget: int
    budget_why: dict = field(default_factory=dict)   # 预算判成了哪个格子，进日志用来回看判得准不准
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    attempts: int = 0
    result: Optional[dict] = None          # {"bubbles": [...]} 或 {"no_reply": True, "reason": ...}
    tool_log: List[dict] = field(default_factory=list)
    rejects: List[str] = field(default_factory=list)   # 每次退回的原因；没有它就分不清重跑是超预算还是撞重复


class Gateway:
    def __init__(self, cfg: dict, data_dir: str, workspace_dir: str, dsh_factory: Callable):
        self.cfg, self.data, self.ws = cfg, data_dir, workspace_dir
        self.dsh_factory = dsh_factory
        self.dsh = None
        self.dsh_epoch = ""
        # 同一会话一把锁（保序 + primed/seen/recent 状态不打架），总并发由信号量封顶。
        # max_concurrent=1 时等价于改造前的全局串行。
        self._conv_locks: Dict[str, threading.Lock] = {}
        self._conv_locks_guard = threading.Lock()   # 建锁本身要互斥，否则同名会话可能各拿到一把
        self._slots = threading.BoundedSemaphore(max(1, int(cfg.get("max_concurrent") or 1)))
        self._dsh_guard = threading.RLock()         # 建/重建 dsh 进程要互斥
        self.inflight: Dict[str, Inflight] = {}     # turn_id -> Inflight（在飞的轮次）
        self._inflight_guard = threading.RLock()
        self._timeout_streak = 0                    # 连续超时轮数，攒够了才重建 dsh
        self.recent = RecentReplies(os.path.join(data_dir, "log", "recent.json"),
                                    cfg["recent_global"], cfg["recent_per_conversation"])
        self.proposals = Proposals(workspace_dir)
        self.skill_index = context.load_skill_index(workspace_dir)
        self.primed = set()
        # 每轮都带历史（用户 2026-09-06 拍板，见 context.select_new）：按会话记住模型已看过哪些流水的 fingerprint，
        # 之后每轮只带没看过的。有界，免得长会话把内存吃光。
        self.seen: dict = {}
        self.started_at = time.time()
        self.turns = 0

    # ---- 会话锁 ----
    def _conv_lock(self, conv: str) -> threading.Lock:
        with self._conv_locks_guard:
            if conv not in self._conv_locks:
                self._conv_locks[conv] = threading.Lock()
            return self._conv_locks[conv]

    # ---- dsh 生命周期 ----
    def _ensure_dsh(self):
        if self.dsh is not None and self.dsh.alive():
            return
        with self._dsh_guard:
            self._ensure_dsh_locked()

    def _ensure_dsh_locked(self):
        # 并发下多个线程会同时发现 dsh 为 None，抢进来的第一个建好之后，后面的直接用
        if self.dsh is not None and self.dsh.alive():
            return
        if self.dsh is not None:
            # 死掉的客户端也要 stop：关掉管道句柄，别泄漏
            try:
                self.dsh.stop()
            except Exception:
                pass
            self.dsh = None
        new = self.dsh_factory()
        try:
            new.start()
            new.initialize(self.ws, self.cfg["provider"], self.cfg["model"])
        except Exception:
            # initialize 失败就别留半死的进程挂在 self.dsh 上
            try:
                new.stop()
            except Exception:
                pass
            self.dsh = None
            raise
        self.dsh = new
        self.dsh_epoch = time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
        self.primed.clear()   # 进程重来了，会话是否接续未知，保守地允许再预热一次
        self.seen.clear()

    def _session_id(self, conversation: str) -> str:
        """返回复合会话 ID：对话名#epoch，防止 dsh 重启后的会话 ID 碰撞。"""
        return f"{conversation}#{self.dsh_epoch}"

    def _restart_dsh_after_timeout(self) -> bool:
        """一轮超时后的处置。返回「是否真的重建了进程」。

        改造前（串行时代）是一超时就 stop 整个 dsh：那一轮还在后台跑，迟到的工具回调会串到
        下一条消息上，turn_id 校验是第一道、重建进程是第二道保险。sdk 至今没有 session/cancel
        （2026-09-05 实测，09-08 复核仍然没有），所以「只取消那一轮」做不到。

        并发之后这么干是错的：**一个会话超时会把别人正在跑的轮次一起杀掉**。改成——
        - 超时的那一轮从在飞集合里摘掉（handle_reply 的 finally 做），迟到回调自然被 STALE_TURN 拒；
        - 只有 dsh 进程真死了，或连续 restart_after_timeouts 轮超时（说明不是个别轮次的事），才重建；
        - 重建时若还有别的轮次在飞，让它们跑完/超时，这次不动手（下一次超时再判）。
        """
        with self._dsh_guard:
            dead = self.dsh is None or not self.dsh.alive()
            streak_hit = self._timeout_streak >= max(1, int(self.cfg.get("restart_after_timeouts") or 1))
            if not dead and not streak_hit:
                return False
            with self._inflight_guard:
                others = len(self.inflight)
            if not dead and others > 1:
                # 别人还在飞，杀进程会连坐；等下一次超时再说（streak 不清零，下次照样命中）
                return False
            if self.dsh is not None:
                try:
                    self.dsh.stop()
                except Exception:
                    pass
            self.dsh = None
            self._timeout_streak = 0
            return True

    # ---- 主流程 ----
    def handle_reply(self, payload: dict) -> dict:
        conv = str(payload.get("conversation", "")).strip()
        is_group = bool(payload.get("is_group"))
        sender = str(payload.get("sender", "")).strip() or conv
        text = str(payload.get("text", ""))
        if not conv:
            return {"error": "conversation 必填"}
        try:
            attempt = max(1, int(payload.get("attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
        if not text.strip() or text.strip() in ("[动画表情]", "[图片]", "[视频]", "[文件]", "[链接]"):
            # 空输入不进大脑：09-05 hzfood 日志里一条空消息让 dsh 把网关源码写成了"开发汇报"
            return {"no_reply": True, "reason": "empty"}
        # 两道闸：先抢一个并发名额（总量封顶），再拿这个会话自己的锁（同一会话严格串行、保序）。
        # 顺序不能反 —— 先拿会话锁再等名额的话，同一会话排队的请求会把名额攥在手里空等。
        wait = float(self.cfg["lock_timeout_sec"])
        deadline = time.time() + wait
        if not self._slots.acquire(timeout=wait):
            return {"error": "busy"}
        clock = self._conv_lock(conv)
        if not clock.acquire(timeout=max(0.0, deadline - time.time())):
            self._slots.release()
            return {"error": "busy"}
        t0 = time.time()
        inf = None
        try:
            try:
                self._ensure_dsh()
            except Exception as e:
                return {"error": f"dsh init failed: {type(e).__name__}: {e}"}
            raw_hist = list(payload.get("prime") or [])
            first_time = conv not in self.primed
            if first_time:
                prime = context.filter_prime(raw_hist, self.cfg["prime_count"], is_group) or None
                label = context.PRIME_LABEL
            else:
                prime = context.select_new(raw_hist, self.seen.get(conv, set()), is_group, exclude=(sender, text),
                                           count=self.cfg["history_delta_max"]) or None
                label = context.DELTA_LABEL
            skills = context.match_skills(self.skill_index, conv, is_group)
            # 预算按「办事/闲聊 × 想不想深入」两维算，两维都从对方的消息里取（raw_hist 在上面已就绪），
            # 模型不参与判定 —— 模型自评长度会系统性漂向长档。见 shape.budget_detail 与本次设计文档。
            _budget, _why = shape.budget_detail(text, is_group, self.cfg, raw_hist, sender)
            inf = Inflight(conv, is_group, _budget, _why)
            with self._inflight_guard:
                self.inflight[inf.turn_id] = inf
            body = context.build_user_message(conv, is_group, sender, text, time.strftime("%Y-%m-%d %H:%M"),
                                              skills, prime, prime_label=label)
            if attempt > 1:
                # 机器人侧 dsh_brain 失败后再起的一轮（2026-09-07）：上一轮多半是工具报错/超时耗光了时间，
                # 这轮要它收着点，别把同一套工具再跑一遍。
                body += (f"\n[系统提示：这条消息上一轮处理超时或出错，这是第 {attempt} 次尝试。"
                         "少调工具：同一个工具报错 2 次就停，用已有信息直接回复，回不了就说清楚哪步卡住]")
            msg = _stamp_turn_id(body, inf.turn_id)
            session_id = self._session_id(conv)
            turn = self.dsh.prompt(session_id, msg, self.cfg["turn_timeout_sec"])
            reasoning = turn.reasoning
            if inf.result is None and not turn.timed_out and not turn.error:
                turn2 = self.dsh.prompt(session_id, NUDGE, self.cfg["turn_timeout_sec"])
                reasoning += "\n---nudge---\n" + turn2.reasoning
                turn.timed_out = turn2.timed_out
                turn.error = turn.error or turn2.error   # nudge 那轮的 dsh 错误同样如实上报
            restarted = False
            if turn.timed_out:
                # 超时的那一轮 dsh 还在后台跑，迟到的 wx_reply 会串到别的消息上；
                # 第一道防线是 turn_id（finally 里把本轮摘掉，迟到回调就找不到归属了），
                # 重建进程只在「进程真死」或「连续超时」时才做，否则并发下会误杀别人在飞的轮次。
                self._timeout_streak += 1
                restarted = self._restart_dsh_after_timeout()
            else:
                self._timeout_streak = 0
            if not turn.error and not turn.timed_out:
                # 模型真的看到了这轮的消息才记作"已看过"；dsh 报错/超时的那轮下次要再带一遍
                self.primed.add(conv)
                self._mark_seen(conv, raw_hist)
            result = inf.result
            if result is None:
                # 故障不许伪装成「不想说话」：模型没调 no_reply，是它压根没跑成。
                # 报 error，机器人侧 dsh_brain 就会返回失败串走 model_fallback 换老接口顶上（CLAUDE.md 3.21）；
                # 报 no_reply 则是静默失聋 —— 2026-09-06 14:11 songkey 额度耗尽 → dsh 403 → 这里 no_reply →
                # 群里被 @ 也不吭声，机器人日志还打成"AI 判断无需接话"，从后台完全看不出是故障。
                reason = "dsh_error" if turn.error else ("timeout" if turn.timed_out else "model_silent")
                result = {"error": reason, "reason": reason}
            if result.get("bubbles"):
                self.recent.add(conv, result["bubbles"])
            truncated = memory_guard.enforce(os.path.join(self.ws, "memory"), self.cfg["memory_max_bytes"])
            self.turns += 1
            dsh_tools = [str(((ev.get("data") or {}).get("name")) or ((ev.get("data") or {}).get("tool")) or "?")
                         for ev in turn.events if ev.get("type") == "tool/call"]   # 含 MCP 工具（hzfood/grok），排障用
            log_rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "conversation": conv, "is_group": is_group,
                       "sender": sender, "text": text, "budget": inf.budget, "budget_why": inf.budget_why, "skills": skills,
                       "turn_id": inf.turn_id, "session_id": session_id,
                       "primed": first_time and prime is not None, "history_new": 0 if first_time else len(prime or []),
                       "result": result, "attempts": inf.attempts, "rejects": inf.rejects, "attempt": attempt,
                       "tools": inf.tool_log, "reasoning": reasoning[:2000], "draft": turn.text[:1000],
                       "memory_truncated": truncated, "dsh_restarted_after_timeout": restarted, "dsh_tools": dsh_tools,
                       "ms": int((time.time() - t0) * 1000)}
            if turn.error:
                log_rec["dsh_error"] = turn.error
            self._log(log_rec)
            return result
        finally:
            if inf is not None:
                with self._inflight_guard:
                    self.inflight.pop(inf.turn_id, None)
            clock.release()
            self._slots.release()

    def _mark_seen(self, conv: str, items: list) -> None:
        """这次传来的历史（含被过滤掉的）全部记作已看过：比预热更早的旧消息以后也不该再冒出来。"""
        seen = self.seen.setdefault(conv, set())
        seen.update(context.fingerprint(x) for x in items)
        if len(seen) > self.cfg["seen_max_per_conversation"]:
            # 机器人每次只传最近 60 条，早于这一批的 fingerprint 不会再来，随便丢一半即可
            for fp in list(seen)[: len(seen) // 2]:
                seen.discard(fp)

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
    def _resolve_inflight(self, name: str, args: dict):
        """把一次工具回调归属到某一轮。返回 (Inflight|None, 拒绝原因|None)。

        规则（并发下的串台防线，见 SPEC §2.2）：
        - 带了 turn_id：必须精确命中在飞的某一轮，否则拒（上一轮超时后 dsh 迟到的回调走这里）。
        - 没带 turn_id：只有在飞恰好一轮时才回落到它（兼容单轮场景与老调用方）；
          在飞 ≥2 轮时无从归属，一律拒 —— 放行就是并发串台的正门。
        """
        tid = args.get("turn_id")
        with self._inflight_guard:
            if tid is not None:
                inf = self.inflight.get(str(tid))
                return (inf, None) if inf is not None else (None, STALE_TURN)
            if not self.inflight:
                return None, "当前没有在处理的消息，这个调用被忽略。"
            if len(self.inflight) > 1:
                return None, AMBIGUOUS_TURN
            only = next(iter(self.inflight.values()))
        only.tool_log.append({"tool": name, "no_turn_id": True})
        return only, None

    def tool_call(self, name: str, args: dict) -> dict:
        inf, reject = self._resolve_inflight(name, args)
        if name == "kb_search":
            # 检索本身不依赖归属：归属不上也照查，只是这一笔记不进 tool_log
            txt = kb.search(self.cfg["kb_url"], str(args.get("query", "")), self.cfg["kb_timeout_sec"])
            if inf is not None:
                inf.tool_log.append({"tool": name, "query": args.get("query", ""),
                                     "ok": not txt.startswith("检索不可用"), "chars": len(txt)})
            return {"ok": True, "text": txt}
        if inf is None:
            return {"ok": False, "text": reject}
        inf.tool_log.append({"tool": name, "args": args})
        if name == "wx_reply":
            inf.attempts += 1
            accepted, err = validate_reply(args.get("bubbles") or [], inf.is_group, inf.budget,
                                           self.recent.recent_for(inf.conversation), inf.attempts, self.cfg)
            if err:
                inf.rejects.append(err[:120])
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
                "pending_proposals": len(self.proposals.list("pending")),
                "inflight": len(self.inflight), "max_concurrent": int(self.cfg.get("max_concurrent") or 1),
                "busy": len(self.inflight) >= int(self.cfg.get("max_concurrent") or 1)}


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
            try:
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
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
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
