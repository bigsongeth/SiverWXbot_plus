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
import sys
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
    error: str = ""


class DshClient:
    def __init__(self, argv: List[str], cwd: str, env: dict, stderr_path: Optional[str] = None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.stderr_path = stderr_path
        self.p: Optional[subprocess.Popen] = None
        self._id = 0
        self._pending: Dict[int, queue.Queue] = {}
        self._notes: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr_f = None   # 真文件对象时才持有，_close_pipes 里一并关掉
        self._reader_t: Optional[threading.Thread] = None

    # ---- 进程 ----
    def start(self) -> None:
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        self._stderr_f = err if self.stderr_path else None
        try:
            self.p = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", bufsize=1)
        except Exception:
            self._close_stderr()
            raise
        self._reader_t = threading.Thread(target=self._reader, daemon=True)
        self._reader_t.start()

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def stop(self) -> None:
        if self.p is None:
            return
        if self.alive():
            # graceful shutdown 请求跑在后台线程里：_request 的 timeout 只挡得住等回包，
            # 挡不住前面那次阻塞的 stdin.write（进程卡死、管道写满时会永久阻塞）。
            # 主线程只等这个线程最多 3 秒，超时就不再等，直接走强制路径。
            shutdown_done = threading.Event()

            def _do_shutdown() -> None:
                try:
                    self._request("shutdown", {}, timeout=3)
                except Exception:
                    pass
                finally:
                    shutdown_done.set()

            threading.Thread(target=_do_shutdown, daemon=True).start()
            shutdown_done.wait(timeout=3)
            try:
                self.p.terminate()
                self.p.wait(timeout=3)
            except Exception:
                try:
                    self.p.kill()
                    self.p.wait(timeout=3)
                except Exception:
                    pass
        # 读线程还卡在 readline 里时绝不能关 stdout：BufferedReader.close 会等它那把锁，
        # 而子进程的孙进程（MCP 服务）若继承了管道写端，读线程要等它们全退出才拿到 EOF——
        # 2026-09-06 11:04 一轮超时后 stop() 就这样卡了 12 分钟，整个网关跟着停摆。
        t = self._reader_t
        if t is not None:
            t.join(timeout=5)
        self._close_pipes(close_stdout=(t is None or not t.is_alive()))

    def _close_pipes(self, close_stdout: bool = True) -> None:
        # 崩溃路径（进程自己退出）和正常路径都要走到这里，否则 stdin/stdout 的
        # TextIOWrapper 会一直不关，触发 ResourceWarning: unclosed file。
        self._close_stderr()
        if self.p is None:
            return
        files = [self.p.stdin] + ([self.p.stdout] if close_stdout else [])
        for f in files:
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass
        if not close_stdout:
            # 读线程会在 EOF 到来时自己退出；这里宁可漏关一个 fd，也不能让 stop() 阻塞。
            sys.stderr.write("[dsh_client] stdout 读线程未退出（孙进程仍持有管道），跳过关闭\n")

    def _close_stderr(self) -> None:
        f, self._stderr_f = self._stderr_f, None
        if f is not None and hasattr(f, "close"):
            try:
                f.close()
            except Exception:
                pass

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
        # 写锁只保这一段：并发的 _request 若不加锁，两条 frame 的 write+flush 可能交错，
        # 把换行分帧的协议写坏（尤其是 frame 超过 PIPE_BUF 时）。
        with self._write_lock:
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

    # 注意：sdk 运行时（dsh-sdk-jsonrpc-server 0.1.2-rc.1）只认 initialize / session/prompt / shutdown
    # 三个方法，2026-09-05 实测 session/cancel、session/control 都回 "unknown ... runtime method"。
    # 所以一轮超时后没法只取消那一轮，只能由网关重建整个进程。

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
                    reason = (ev.get("data") or {}).get("reason") or {}
                    kind = reason.get("kind")
                    if kind in ("error", "aborted"):
                        err = reason.get("error") or {}
                        # message 为空或 aborted 也要留痕，否则会被上层当成 model_silent
                        res.error = err.get("message") or f"turn/end kind={kind}"
                        if err.get("code"):
                            res.error += f" (code={err['code']})"
            elif meth == "session.status" and prm.get("status") == "idle" and turn_ended:
                return res
        res.timed_out = True
        return res
