# -*- coding: utf-8 -*-
"""假 dsh sdk 服务：stdin 读 JSON-RPC 行，模拟 initialize / session/prompt / shutdown。
prompt 文本里含 "SLOW" 时 sleep 3 秒再回；含 "CRASH" 时直接退出进程；
含 "HANG" 时回一次 ack 后不再读 stdin、直接 sleep 30 秒（模拟进程卡死不响应 shutdown）；
含 "TURNERR" 时发一个带错误的 turn/end 事件；
含 "HANGCHILD" 时先起一个继承 stdout 的孙进程（sleep 60）再卡死——模拟 dsh 被 kill 后管道写端仍被 MCP 子进程持有。"""
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
        if "HANGCHILD" in text:
            import subprocess
            subprocess.Popen(["sleep", "60"])   # 继承本进程的 stdout（管道写端）
        if "HANG" in text:
            # 卡死模拟：不再发任何事件，也不再读下一行 stdin（本次循环体内同步 sleep）。
            time.sleep(30)
            continue
        out({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": s, "status": "running"}})
        event(s, "turn/start", {"turn": 1})
        if "SLOW" in text:
            time.sleep(3)
        if "TURNERR" in text:
            event(s, "turn/end", {"turn": 1, "reason": {"kind": "error", "error": {"message": "boom"}}})
        else:
            event(s, "assistant/message", {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": "thinking about: " + text},
                {"type": "text", "text": "echo: " + text}]}})
            event(s, "turn/end", {"turn": 1})
        out({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": s, "status": "idle"}})
    elif m == "shutdown":
        out({"jsonrpc": "2.0", "id": i, "result": {}})
        sys.exit(0)
