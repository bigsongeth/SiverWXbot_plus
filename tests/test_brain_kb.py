# -*- coding: utf-8 -*-
"""kb.search：必须无视环境里的 HTTP_PROXY 直连（2026-09-05 回放里几十次检索全被代理吃掉）。"""
from __future__ import annotations
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from brain.gateway import kb


class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        out = json.dumps({"context": "片段:" + body["query"], "is_ncc": True,
                          "facts": "【固定事实清单】\n大理", "meta": {}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class KbTest(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_search_ignores_proxy_env(self):
        # 代理指到一个没人听的端口；不绕开的话 urllib 会去连它然后失败
        env = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        with patch.dict(os.environ, env):
            txt = kb.search(self.url, "黑多岛还能去吗", 5)
        self.assertIn("片段:黑多岛还能去吗", txt)
        self.assertIn("固定事实清单", txt)

    def test_unavailable_returns_hint(self):
        txt = kb.search("http://127.0.0.1:9", "x", 2)
        self.assertTrue(txt.startswith("检索不可用"))


if __name__ == "__main__":
    unittest.main()
