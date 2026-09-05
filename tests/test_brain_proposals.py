# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from brain.gateway import kb, memory_guard
from brain.gateway.proposals import Proposals


class ProposalsTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "knowledge"))
        with open(os.path.join(self.ws, "knowledge", "shared.md"), "w", encoding="utf-8") as f:
            f.write("# 共享知识\n")
        self.p = Proposals(self.ws)

    def test_create_list_approve(self):
        pid = self.p.create("大理下月涨到 3000", "松爸在私聊说的", "松爸")
        self.assertEqual([x["id"] for x in self.p.list("pending")], [pid])
        self.p.approve(pid)
        self.assertEqual(self.p.list("pending"), [])
        with open(os.path.join(self.ws, "knowledge", "shared.md"), encoding="utf-8") as f:
            shared = f.read()
        self.assertIn("来源: 松爸在私聊说的) 大理下月涨到 3000", shared)

    def test_reject_does_not_touch_shared(self):
        pid = self.p.create("x", "y", "z")
        self.p.reject(pid)
        self.assertEqual(self.p.list("rejected")[0]["id"], pid)
        with open(os.path.join(self.ws, "knowledge", "shared.md"), encoding="utf-8") as f:
            shared = f.read()
        self.assertNotIn("x", shared)

    def test_path_rejects_traversal(self):
        with self.assertRaises(ValueError) as ctx:
            self.p.approve("../../etc/x")
        self.assertIn("非法的提议 id", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.p.reject("../etc/passwd")
        self.assertIn("非法的提议 id", str(ctx.exception))

    def test_approve_does_not_resurrect_rejected(self):
        pid = self.p.create("test proposal", "source", "conv")
        self.p.reject(pid)
        self.assertEqual(self.p._read(pid)["status"], "rejected")
        self.p.approve(pid)
        self.assertEqual(self.p._read(pid)["status"], "rejected")
        with open(os.path.join(self.ws, "knowledge", "shared.md"), encoding="utf-8") as f:
            shared = f.read()
        self.assertNotIn("test proposal", shared)


class MemoryGuardTest(unittest.TestCase):
    def test_truncates_oversized(self):
        d = tempfile.mkdtemp()
        big = os.path.join(d, "松爸.md")
        with open(big, "w", encoding="utf-8") as f:
            f.write("行\n" * 3000)
        small = os.path.join(d, "小.md")
        with open(small, "w", encoding="utf-8") as f:
            f.write("ok")
        out = memory_guard.enforce(d, 4096)
        self.assertEqual(out, [big])
        with open(big, "rb") as f:
            data = f.read()
        self.assertLessEqual(len(data), 4096 + 40)
        self.assertTrue(data.decode("utf-8").endswith("[已截尾]\n"))


class KbTest(unittest.TestCase):
    def test_search_formats_context_and_facts(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                out = json.dumps({"context": "[1] 大理据点在古城", "is_ncc": True, "facts": "自营：大理、黄山",
                                  "meta": {"trigger": "关键词"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        txt = kb.search(f"http://127.0.0.1:{srv.server_port}", "大理", 5)
        srv.shutdown()
        srv.server_close()
        self.assertTrue(txt.startswith("【固定事实清单（优先级最高）】\n自营：大理、黄山"))
        self.assertIn("【检索片段】\n[1] 大理据点在古城", txt)

    def test_search_unavailable(self):
        txt = kb.search("http://127.0.0.1:9", "x", 1)
        self.assertTrue(txt.startswith("检索不可用"))

    def test_kb_search_clips_oversized_fields(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                out = json.dumps({
                    "context": "x" * 20000,
                    "is_ncc": True,
                    "facts": "y" * 8000,
                    "meta": {"trigger": "关键词"}
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        txt = kb.search(f"http://127.0.0.1:{srv.server_port}", "test", 5)
        srv.shutdown()
        srv.server_close()
        self.assertIn("（已截断）", txt)
        self.assertEqual(txt.count("（已截断）"), 2)
        self.assertLess(len(txt), 20000)


if __name__ == "__main__":
    unittest.main()
