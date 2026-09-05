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
        cls.p.kill()
        cls.p.wait()
        cls.p.stdin.close()
        cls.p.stdout.close()
        cls.srv.shutdown()
        cls.srv.server_close()

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

    def test_notification_produces_no_response_frame(self):
        # notifications/initialized：带 method、不带 id，按 JSON-RPC 语义是通知，不应有响应帧。
        # 紧跟着发一个 ping，下一行 stdout 读到的应该就是 ping 的响应（id 对得上），
        # 说明桥没有为通知写任何一行 stdout。
        notice = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.p.stdin.write(json.dumps(notice) + "\n")
        self.p.stdin.flush()
        r = self.rpc(100, "ping")
        self.assertEqual(r["id"], 100)
        self.assertEqual(r["result"], {})

    def test_gateway_unreachable_returns_error_and_process_survives(self):
        # 起第二个桥子进程，指向一个没有监听方的端口（127.0.0.1:9 是 discard 服务，
        # 大多数平台上直接拒绝连接），验证网关不可达时返回 isError=true 且进程不死。
        env = dict(os.environ, FEIROU_GW="http://127.0.0.1:9")
        script = os.path.join(os.path.dirname(__file__), "..", "brain", "mcp", "feirou_tools.py")
        p = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, encoding="utf-8", bufsize=1, env=env)
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/call",
                                       "params": {"name": "wx_reply", "arguments": {"bubbles": ["hi"]}}}) + "\n")
            p.stdin.flush()
            r = json.loads(p.stdout.readline())
            self.assertTrue(r["result"]["isError"])
            self.assertTrue(r["result"]["content"][0]["text"].startswith("网关不可用"))
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            p.wait()
            p.stdin.close()
            p.stdout.close()

    def test_non_object_top_level_frame_does_not_crash_bridge(self):
        # Critical case: 合法 JSON 但顶层是数组（合法的 JSON-RPC 批量请求形状），
        # 旧实现会在 m.get(...) 上抛 AttributeError 并让 main() 的异常冒出去、进程退出。
        # 发完这一行后紧跟一个 ping，验证 ping 响应正常返回且进程还活着。
        self.p.stdin.write(json.dumps([1, 2, 3]) + "\n")
        self.p.stdin.flush()
        r = self.rpc(7, "ping")
        self.assertEqual(r["id"], 7)
        self.assertEqual(r["result"], {})
        self.assertIsNone(self.p.poll())


if __name__ == "__main__":
    unittest.main()
