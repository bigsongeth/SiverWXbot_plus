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


if __name__ == "__main__":
    unittest.main()
