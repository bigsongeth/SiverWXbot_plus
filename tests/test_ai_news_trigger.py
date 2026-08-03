# -*- coding: utf-8 -*-
"""plugins/ai_news_note/trigger.py 的单测：外部触发 + 失败重试的状态机。

纯 mock，不碰微信、不发消息。mac 上直接跑：
    cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_ai_news_trigger.py

sender.py 在模块级 import wxautox4 / win32*（只在 Windows 上有），所以下面先往
sys.modules 里塞假模块，让 plugins.ai_news_note 能在 mac 上被导入。
"""
import os
import sys
import json
import types
import time
import shutil
import tempfile
import datetime
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 把 Windows 专属依赖 stub 掉（必须在 import 插件之前）----
for name in ("win32clipboard", "win32gui", "win32con", "win32api", "win32process"):
    sys.modules.setdefault(name, types.ModuleType(name))
_wx = types.ModuleType("wxautox4")
_uia = types.ModuleType("wxautox4.uia")
_uia.uiautomation = types.ModuleType("uiautomation")
_wx.uia = _uia
sys.modules.setdefault("wxautox4", _wx)
sys.modules.setdefault("wxautox4.uia", _uia)

from plugins.ai_news_note import trigger   # noqa: E402


class TriggerTestBase(unittest.TestCase):
    def setUp(self):
        # 把请求/结果文件重定向到临时目录，绝不碰真实的 C:\Users\Admin\ai_news
        self.tmp = tempfile.mkdtemp(prefix="ai_news_trigger_test_")
        self._orig = (trigger._DIR, trigger.REQUEST_FILE, trigger.RESULT_FILE,
                      trigger.send_daily_note, trigger.log)
        trigger._DIR = self.tmp
        trigger.REQUEST_FILE = os.path.join(self.tmp, "send_request.flag")
        trigger.RESULT_FILE = os.path.join(self.tmp, "send_result.json")
        self.logs = []
        trigger.log = lambda m: self.logs.append(str(m))
        self.calls = []
        trigger._retry_at, trigger._retry_left = None, 0

    def tearDown(self):
        (trigger._DIR, trigger.REQUEST_FILE, trigger.RESULT_FILE,
         trigger.send_daily_note, trigger.log) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_send(self, *results):
        """让 send_daily_note 依次返回给定结果；用完后重复最后一个。"""
        seq = list(results)

        def fake(bot=None, force=False, source="scheduled"):
            self.calls.append({"force": force, "source": source})
            return seq.pop(0) if len(seq) > 1 else seq[0]
        trigger.send_daily_note = fake

    def result_json(self):
        with open(trigger.RESULT_FILE, encoding="utf-8") as f:
            return json.load(f)

    def write_request(self, age_sec=0):
        with open(trigger.REQUEST_FILE, "w", encoding="utf-8") as f:
            f.write("")
        if age_sec:
            t = time.time() - age_sec
            os.utime(trigger.REQUEST_FILE, (t, t))


class TestStatusMapping(TriggerTestBase):
    def test_成功映射为_ok(self):
        self.assertEqual(trigger._status_of("✅ 日报已发送到 X（11 条）"), "ok")

    def test_失败映射为_failed(self):
        self.assertEqual(trigger._status_of("❌ 发送失败（建笔记）：没拿到焦点"), "failed")

    def test_跳过映射为_skipped(self):
        self.assertEqual(trigger._status_of("⚠️ 今天的日报还没就位"), "skipped")
        self.assertEqual(trigger._status_of("今天已发过，跳过（防重）"), "skipped")


class TestResultFile(TriggerTestBase):
    def test_成功后写出与旧入口一致的结果文件(self):
        self.stub_send("✅ 日报已发送到 群（11 条）")
        trigger.start_day(object())
        d = self.result_json()
        self.assertEqual(d["status"], "ok")
        self.assertIn("日报已发送", d["result"])
        self.assertEqual(d["date"], datetime.date.today().isoformat())
        self.assertIn("at", d)

    def test_发送抛异常也写结果不炸掉(self):
        def boom(bot=None, force=False, source="scheduled"):
            raise RuntimeError("UI 挂了")
        trigger.send_daily_note = boom
        trigger.start_day(object())
        self.assertEqual(self.result_json()["status"], "failed")

    def test_结果文件写不进去也不影响发送(self):
        self.stub_send("✅ 发了")
        trigger.RESULT_FILE = os.path.join(self.tmp, "no_such_dir", "x", "r.json")
        trigger._DIR = os.path.join(self.tmp, "no_such_dir", "x")
        os.makedirs(trigger._DIR, exist_ok=True)
        os.chmod(trigger._DIR, 0o500)          # 只读目录，写入必失败
        try:
            trigger.start_day(object())        # 不应抛异常
        finally:
            os.chmod(trigger._DIR, 0o700)
        self.assertEqual(len(self.calls), 1)


class TestRetry(TriggerTestBase):
    def test_失败后安排重试(self):
        self.stub_send("❌ 发送失败（建笔记）：没拿到焦点")
        trigger.start_day(object())
        self.assertIsNotNone(trigger._retry_at)
        self.assertEqual(trigger._retry_left, trigger.RETRY_MAX - 1)

    def test_成功后不安排重试(self):
        self.stub_send("✅ 日报已发送到 群（11 条）")
        trigger.start_day(object())
        self.assertIsNone(trigger._retry_at)

    def test_今天已发过不重试(self):
        self.stub_send("今天已发过，跳过（防重）")
        trigger.start_day(object())
        self.assertIsNone(trigger._retry_at)

    def test_已禁用不重试(self):
        self.stub_send("已禁用（config.ENABLED=False），跳过")
        trigger.start_day(object())
        self.assertIsNone(trigger._retry_at)

    def test_数据没就位要重试(self):
        # mac-mini 推迟了，隔一会儿再来一次是有意义的
        self.stub_send("⚠️ 今天（2026-08-03）的日报还没就位，当前数据日期：2026-08-02，未发送。")
        trigger.start_day(object())
        self.assertIsNotNone(trigger._retry_at)

    def test_重试到点才跑(self):
        self.stub_send("❌ 失败")
        trigger.start_day(object())
        self.assertEqual(len(self.calls), 1)
        trigger.tick(object())                  # 还没到点
        self.assertEqual(len(self.calls), 1)
        trigger._retry_at = datetime.datetime.now() - datetime.timedelta(seconds=1)
        trigger.tick(object())                  # 到点了
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["source"], "retry")

    def test_重试次数用尽后停手(self):
        self.stub_send("❌ 一直失败")
        trigger.start_day(object())
        for _ in range(trigger.RETRY_MAX + 3):
            if trigger._retry_at:
                trigger._retry_at = datetime.datetime.now() - datetime.timedelta(seconds=1)
            trigger.tick(object())
        # 首次 + RETRY_MAX 次重试，不能无限试下去
        self.assertEqual(len(self.calls), trigger.RETRY_MAX + 1)
        self.assertIsNone(trigger._retry_at)
        self.assertTrue(any("用尽" in m for m in self.logs))

    def test_中途成功就停止重试(self):
        self.stub_send("❌ 失败", "✅ 日报已发送到 群（11 条）")
        trigger.start_day(object())
        trigger._retry_at = datetime.datetime.now() - datetime.timedelta(seconds=1)
        trigger.tick(object())
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(trigger._retry_at)

    def test_重试一律不带force(self):
        # force 会绕过防重，重试链路上绝不能带，否则可能重复发群
        self.stub_send("❌ 失败")
        trigger.start_day(object())
        trigger._retry_at = datetime.datetime.now() - datetime.timedelta(seconds=1)
        trigger.tick(object())
        self.assertTrue(all(c["force"] is False for c in self.calls))


class TestRequest(TriggerTestBase):
    def test_新鲜请求会被执行并删掉文件(self):
        self.stub_send("✅ 发了")
        self.write_request()
        trigger.tick(object())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["source"], "mac-trigger")
        self.assertFalse(os.path.exists(trigger.REQUEST_FILE))

    def test_陈旧请求被丢弃不执行(self):
        self.stub_send("✅ 发了")
        self.write_request(age_sec=trigger.REQUEST_MAX_AGE_SEC + 60)
        trigger.tick(object())
        self.assertEqual(len(self.calls), 0)
        self.assertFalse(os.path.exists(trigger.REQUEST_FILE))   # 陈旧的也要清掉
        self.assertTrue(any("陈旧" in m for m in self.logs))

    def test_没有请求时tick什么都不做(self):
        self.stub_send("✅ 发了")
        trigger.tick(object())
        self.assertEqual(len(self.calls), 0)

    def test_请求执行失败也会安排重试(self):
        self.stub_send("❌ 发送失败（建笔记）：没拿到焦点")
        self.write_request()
        trigger.tick(object())
        self.assertIsNotNone(trigger._retry_at)

    def test_同一个请求不会被重复执行(self):
        self.stub_send("✅ 发了")
        self.write_request()
        trigger.tick(object())
        trigger.tick(object())
        self.assertEqual(len(self.calls), 1)

    def test_tick出错不会把主循环带崩(self):
        # tick 每 10 秒跑一次，抛异常会污染 bot 主循环
        def boom(*a, **k):
            raise RuntimeError("炸了")
        orig = trigger._take_request
        trigger._take_request = boom
        try:
            trigger.tick(object())          # 不应抛出
        finally:
            trigger._take_request = orig
        self.assertTrue(any("tick 出错" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
