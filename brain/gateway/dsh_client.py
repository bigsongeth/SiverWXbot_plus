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
