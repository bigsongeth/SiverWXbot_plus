# -*- coding: utf-8 -*-
"""model_fallback 插件单测：纯 mock，不发任何请求、不碰微信。

跑法（mac 上别用 -m unittest，anaconda 自带的 tests 包会遮蔽本项目的 tests/）：
    cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_model_fallback.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins.model_fallback as mf
from plugins.model_fallback import chain

API_ERROR_TEXT = chain.API_ERROR_TEXT


class FakeAPI:
    """长得像 wxbot_core 里的 API 类：有 chat/base_url/DS_NOW_MOD/api_key。"""

    def __init__(self, name, behavior="ok", model=None, url=None, key="sk-fakekey0000"):
        self.name = name
        self.behavior = behavior          # ok | raise | error_text | empty
        self.DS_NOW_MOD = model or f"model-{name}"
        self.base_url = url or f"https://{name}.example.com"
        self.api_key = key
        self.calls = []

    def chat(self, message, model=None, stream=True, prompt=None, history=None,
             image_path="", image_url=""):
        self.calls.append({"message": message, "prompt": prompt, "history": history,
                           "image_path": image_path})
        if self.behavior == "raise":
            raise RuntimeError("Error code: 529 - overloaded")
        if self.behavior == "error_text":
            return API_ERROR_TEXT
        if self.behavior == "empty":
            return "   "
        return f"{self.name} 的回答：{message}"


class NoImageAPI(FakeAPI):
    """签名里没有 image_path/image_url 的接口（DifyAPI / CozeAPI 就是这样）。"""

    def chat(self, message, model=None, stream=True, prompt=None, history=None):
        self.calls.append({"message": message, "prompt": prompt, "history": history})
        if self.behavior == "raise":
            raise RuntimeError("boom")
        return f"{self.name} 的回答：{message}"


class FakeBot:
    def __init__(self, raw_config, instances):
        class _Cfg:
            pass
        self.config = _Cfg()
        self.config.config = raw_config
        self._instances = instances          # {索引: FakeAPI}
        self.built = []                      # 记录实际实例化过哪些索引

    def _init_api_by_index(self, idx):
        self.built.append(idx)
        api = self._instances[idx]
        if isinstance(api, Exception):
            raise api
        return api


def silence_logs():
    chain._log = lambda level, message: None


class FailureDetectionTest(unittest.TestCase):
    def test_底层吞异常后的固定串算失败(self):
        self.assertTrue(chain.is_failure(API_ERROR_TEXT))

    def test_空值与空白串算失败(self):
        self.assertTrue(chain.is_failure(None))
        self.assertTrue(chain.is_failure(""))
        self.assertTrue(chain.is_failure("   "))

    def test_正常回复不算失败(self):
        self.assertFalse(chain.is_failure("今天天气不错"))


class FallbackChatTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        mf.reset_cache()

    def _wrap(self, primary, backups):
        factories = [(b.name, (lambda x=b: x)) for b in backups]
        return chain.FallbackAPI(primary, factories, "测试群")

    def test_主接口成功时不碰备用(self):
        primary, backup = FakeAPI("主"), FakeAPI("备")
        reply = self._wrap(primary, [backup]).chat("你好")
        self.assertEqual(reply, "主 的回答：你好")
        self.assertEqual(backup.calls, [])

    def test_主接口抛异常时切备用(self):
        primary, backup = FakeAPI("主", "raise"), FakeAPI("备")
        reply = self._wrap(primary, [backup]).chat("你好")
        self.assertEqual(reply, "备 的回答：你好")

    def test_主接口返回错误占位串时切备用(self):
        primary, backup = FakeAPI("主", "error_text"), FakeAPI("备")
        self.assertEqual(self._wrap(primary, [backup]).chat("你好"), "备 的回答：你好")

    def test_主接口返回空白时切备用(self):
        primary, backup = FakeAPI("主", "empty"), FakeAPI("备")
        self.assertEqual(self._wrap(primary, [backup]).chat("你好"), "备 的回答：你好")

    def test_按顺序试直到第一个成功(self):
        primary = FakeAPI("主", "raise")
        b1, b2, b3 = FakeAPI("备1", "raise"), FakeAPI("备2"), FakeAPI("备3")
        reply = self._wrap(primary, [b1, b2, b3]).chat("你好")
        self.assertEqual(reply, "备2 的回答：你好")
        self.assertEqual(b3.calls, [], "已经成功了就不该继续往下试")

    def test_全链失败时交回上层的固定串(self):
        primary = FakeAPI("主", "raise")
        b1, b2 = FakeAPI("备1", "raise"), FakeAPI("备2", "error_text")
        reply = self._wrap(primary, [b1, b2]).chat("你好")
        self.assertEqual(reply, API_ERROR_TEXT,
                         "必须交回上层认识的串，后续照旧走 api_error_reply")

    def test_同一个接口不重复试(self):
        primary = FakeAPI("主", "raise")
        same = FakeAPI("链上的主", "raise", model=primary.DS_NOW_MOD,
                       url=primary.base_url, key=primary.api_key)
        other = FakeAPI("备")
        reply = self._wrap(primary, [same, other]).chat("你好")
        self.assertEqual(reply, "备 的回答：你好")
        self.assertEqual(same.calls, [], "身份与主接口相同，应被跳过")

    def test_原样透传人设与历史(self):
        primary, backup = FakeAPI("主", "raise"), FakeAPI("备")
        history = [{"role": "user", "content": "上一个问题"}]
        self._wrap(primary, [backup]).chat("你好", prompt="人设X", history=history)
        self.assertEqual(backup.calls[0]["prompt"], "人设X")
        self.assertEqual(backup.calls[0]["history"], history,
                         "备用接口要拿着同样的上下文继续回答上一个问题")

    def test_备用接口不支持图片参数时自动剔除(self):
        primary = FakeAPI("主", "raise")
        backup = NoImageAPI("备")
        reply = self._wrap(primary, [backup]).chat("看图", image_path="/tmp/a.jpg")
        self.assertEqual(reply, "备 的回答：看图",
                         "签名不兼容不该被算成一次失败")

    def test_备用实例化失败时跳过继续下一个(self):
        primary = FakeAPI("主", "raise")
        good = FakeAPI("备2")

        def _boom():
            raise ValueError("配置写错了")

        wrapped = chain.FallbackAPI(primary, [("备1", _boom), ("备2", lambda: good)], "群")
        self.assertEqual(wrapped.chat("你好"), "备2 的回答：你好")

    def test_其余属性代理到主接口(self):
        primary = FakeAPI("主")
        self.assertEqual(self._wrap(primary, []).DS_NOW_MOD, "model-主")


class LoggingTest(unittest.TestCase):
    """日志是这个功能唯一的事后排查手段，切换过程必须留痕。"""

    def setUp(self):
        self.records = []
        chain._log = lambda level, message: self.records.append((level, message))

    def tearDown(self):
        silence_logs()

    def _text(self):
        return "\n".join(m for _, m in self.records)

    def test_切换成功时记下会话_失败原因_和最终用了哪个接口(self):
        primary = FakeAPI("主", "raise", model="grok-4.5")
        backup = FakeAPI("备", model="glm-5.2")
        wrapped = chain.FallbackAPI(primary, [("接口2(glm-5.2)", lambda: backup)], "松爸")
        wrapped.chat("你好")
        text = self._text()
        self.assertIn("[松爸]", text, "日志要能认出是哪个会话")
        self.assertIn("grok-4.5", text, "要记下是哪个模型挂了")
        self.assertIn("529", text, "要保留上游的错误信息")
        self.assertIn("glm-5.2", text, "要记下最终换成了谁")
        self.assertTrue(any(lv == "SUCCESS" for lv, _ in self.records))

    def test_底层吞异常时提示去看上一条接口日志(self):
        primary = FakeAPI("主", "error_text")
        wrapped = chain.FallbackAPI(primary, [("接口2", lambda: FakeAPI("备"))], "某群")
        wrapped.chat("你好")
        self.assertIn(chain.PLACEHOLDER_HINT, self._text())

    def test_全链失败记一条ERROR(self):
        primary = FakeAPI("主", "raise")
        wrapped = chain.FallbackAPI(primary, [("接口2", lambda: FakeAPI("备", "raise"))], "某群")
        wrapped.chat("你好")
        self.assertTrue(any(lv == "ERROR" for lv, _ in self.records))

    def test_超长报错被截断(self):
        class HugeErrorAPI(FakeAPI):
            def chat(self, message, **kw):
                raise RuntimeError("<html>" + "x" * 5000 + "</html>")

        wrapped = chain.FallbackAPI(HugeErrorAPI("主"), [], "某群")
        wrapped.chat("你好")
        longest = max(len(m) for _, m in self.records)
        self.assertLess(longest, 400, "上游返回整页 HTML 时不该把日志刷爆")

    def test_日志里不出现完整密钥(self):
        primary = FakeAPI("主", "raise", key="sk-verysecretkey-123456789")
        wrapped = chain.FallbackAPI(primary, [("接口2", lambda: FakeAPI("备"))], "某群")
        wrapped.chat("你好")
        self.assertNotIn("sk-verysecretkey-123456789", self._text())


class BuildBackupFactoriesTest(unittest.TestCase):
    def setUp(self):
        silence_logs()

    def _cfg(self, **kw):
        base = {
            "fallback_switch": True,
            "fallback_chain": [1, 2],
            "api_configs": [
                {"sdk": "OpenAI SDK", "model": "m0"},
                {"sdk": "OpenAI SDK", "model": "m1"},
                {"sdk": "DusAPI", "model": "m2"},
            ],
        }
        base.update(kw)
        return base

    def test_开关关闭时不产生备用(self):
        self.assertEqual(
            chain.build_backup_factories(self._cfg(fallback_switch=False), lambda i: i), [])

    def test_按链顺序产出且标签含模型名(self):
        got = chain.build_backup_factories(self._cfg(), lambda i: f"api{i}")
        self.assertEqual([label for label, _ in got], ["接口2(m1)", "接口3(m2)"])
        self.assertEqual([f() for _, f in got], ["api1", "api2"])

    def test_越界与非法索引被跳过(self):
        got = chain.build_backup_factories(
            self._cfg(fallback_chain=[9, -1, "x", 1]), lambda i: f"api{i}")
        self.assertEqual([label for label, _ in got], ["接口2(m1)"])

    def test_重复索引去重(self):
        got = chain.build_backup_factories(
            self._cfg(fallback_chain=[1, 1, 2]), lambda i: f"api{i}")
        self.assertEqual(len(got), 2)

    def test_链不是列表时忽略(self):
        self.assertEqual(
            chain.build_backup_factories(self._cfg(fallback_chain="1,2"), lambda i: i), [])


class WrapTest(unittest.TestCase):
    def setUp(self):
        silence_logs()
        mf.reset_cache()

    def _bot(self, **kw):
        cfg = {
            "fallback_switch": True,
            "fallback_chain": [1],
            "api_configs": [{"sdk": "OpenAI SDK", "model": "m0"},
                            {"sdk": "OpenAI SDK", "model": "m1"}],
        }
        cfg.update(kw)
        return FakeBot(cfg, {1: FakeAPI("备")})

    def test_开关关闭时原样返回不包装(self):
        bot = self._bot(fallback_switch=False)
        primary = FakeAPI("主")
        self.assertIs(mf.wrap(bot, primary, "某群"), primary)

    def test_链为空时原样返回(self):
        bot = self._bot(fallback_chain=[])
        primary = FakeAPI("主")
        self.assertIs(mf.wrap(bot, primary, "某群"), primary)

    def test_开启后返回包装实例并能切备用(self):
        bot = self._bot()
        wrapped = mf.wrap(bot, FakeAPI("主", "raise"), "某群")
        self.assertIsInstance(wrapped, chain.FallbackAPI)
        self.assertEqual(wrapped.chat("你好"), "备 的回答：你好")

    def test_备用接口惰性实例化(self):
        bot = self._bot()
        wrapped = mf.wrap(bot, FakeAPI("主"), "某群")
        wrapped.chat("你好")
        self.assertEqual(bot.built, [], "主接口没失败就不该去 new 备用接口")

    def test_None_原样返回(self):
        self.assertIsNone(mf.wrap(self._bot(), None, "某群"))

    def test_插件自身出错时退回原接口(self):
        class BrokenBot:
            pass
        primary = FakeAPI("主")
        self.assertIs(mf.wrap(BrokenBot(), primary, "某群"), primary)

    def test_同一主接口复用包装实例只换会话名(self):
        bot = self._bot()
        primary = FakeAPI("主")
        first = mf.wrap(bot, primary, "群A")
        second = mf.wrap(bot, primary, "群B")
        self.assertIs(first, second)
        self.assertEqual(second._session_name, "群B")


if __name__ == "__main__":
    unittest.main(verbosity=2)
