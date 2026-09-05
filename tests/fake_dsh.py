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
