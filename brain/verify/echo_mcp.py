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
