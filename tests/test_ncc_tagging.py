# -*- coding: utf-8 -*-
"""群🐶自动打标（方案 A，2026-09-13）单测：discovery.handle_global_group / tagsync.post_tag /
handle_discovery 改造 / 每日汇总 / 「修备注 <群名>」。

全部纯 mock：不碰微信、不外联（webhook_send.send_message 全程被替换）、
登记表 / 插件配置 / 状态文件 / task_runner 请求文件全指到临时目录，
config.json 的同步只用临时文件。跑法：
    cd /Volumes/SiverWXbot_plus-main && PYTHONPATH=. python3 tests/test_ncc_tagging.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

# 单测日志不许写进生产 panel_logs —— 必须在导入插件之前设
os.environ.setdefault("NCC_LOG_SILENT", "1")

from plugins.ncc_community import (audit, discovery, forward, registry, store,  # noqa: E402
                                   tagsync, task_runner)

DOG = "\U0001f436"
ADMIN = "NCC 社群管理肥肉售后维权🤖"
TEST_ADMIN = "爱和一切肥肉测试群"


class FakeWxResponse:
    def __init__(self, ok=True, message=""):
        self.ok, self.message = ok, message

    def __bool__(self):
        return self.ok

    def __getitem__(self, key):
        return {"message": self.message}.get(key)


class FakeWx:
    """主窗口模型：current 是此刻停在哪个会话；remarks 模拟 SetGroupRemark 的【追加】语义，
    ChatInfo 的 chat_name 是当前显示名（有备注就是备注，没有 remark 键——真机实测）。"""
    nickname = "🐶肥肉"

    def __init__(self):
        self.current = None
        self.remarks = {}
        self.chat_types = {}
        self.chatted = []
        self.remark_calls = []
        self.fail_names = set()
        self.remark_takes_effect = True

    def ChatWith(self, who=None, exact=False):
        self.chatted.append(who)
        if who in self.fail_names:
            return FakeWxResponse(False, "未找到会话")
        self.current = who
        return None

    def ChatInfo(self):
        if self.current is None:
            return {}
        return {"chat_name": self.remarks.get(self.current) or self.current,
                "chat_type": self.chat_types.get(self.current, "group"),
                "group_member_count": 3}

    def SetGroupRemark(self, value):
        self.remark_calls.append((self.current, value))
        if self.remark_takes_effect:
            self.remarks[self.current] = self.remarks.get(self.current, "") + value
        return {"status": "成功", "message": None, "data": None}

    def SendMsg(self, msg=None, who=None, **kw):
        return None


class FakeBotConfig:
    """模仿 WXBotConfig：config 是全量 dict，group / 两个 map 与 dict 里的是【同一个对象】
    （wxbot_core 669/788 行就是这么赋值的）。"""

    def __init__(self, config_file, cfg):
        self.CONFIG_FILE = config_file
        self.config = cfg
        self.group = cfg.setdefault("group", [])
        self.group_api_map = cfg.setdefault("group_api_map", {})
        self.group_prompt_map = cfg.setdefault("group_prompt_map", {})
        self.AtMe = "@🐶肥肉"


class FakeMemoryManager:
    def __init__(self, base_path, wx_id):
        self.base_path, self.wx_id = base_path, wx_id

    @classmethod
    def _resolve_storage_name(cls, name):
        return str(name), False


class FakeMsg:
    def __init__(self, content="hi", attr="friend", mtype="text", sender="路人"):
        self.content, self.attr, self.type, self.sender = content, attr, mtype, sender


class FakeChat:
    def __init__(self, who, chat_type="group"):
        self.who, self.chat_type, self.sent = who, chat_type, []

    def SendMsg(self, msg=None, **kw):
        self.sent.append(msg)
        return None


class FakeSchedule:
    """只记录注册了什么。"""

    def __init__(self):
        self.jobs = []
        self.cleared = []

    def clear(self, tag=None):
        self.cleared.append(tag)

    def every(self, n=None):
        sched = self

        class _Job:
            def __init__(self):
                self.at_time, self.func, self.tags = None, None, ()
                self.every_n = n

            @property
            def day(self):
                return self

            @property
            def seconds(self):
                return self

            def at(self, t):
                self.at_time = t
                return self

            def do(self, fn, *a, **kw):
                self.func = (fn, a, kw)
                sched.jobs.append(self)
                return self

            def tag(self, *tags):
                self.tags = tags
                return self
        return _Job()


def _long_name():
    # 17 个汉字 + 🐶 = 55 字节 > 48
    return "十七个字的超长群名称一二三四五六七"


class TaggingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncc_tag_")
        # registry / store / discovery 状态 / task_runner 全部指到临时目录
        self._orig = {
            "reg": (registry.DATA_DIR, registry.REGISTRY_PATH),
            "store": (store.DATA_DIR, store.CONFIG_PATH),
            "state": discovery.STATE_PATH,
            "task": (task_runner.REQUEST_PATH, task_runner.RESULT_PATH),
            "settle": forward.CHATINFO_SETTLE,
            "sleep": forward.REMARK_SETTLE,
        }
        registry.DATA_DIR = self.tmp
        registry.REGISTRY_PATH = os.path.join(self.tmp, "registry.json")
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None
        discovery.STATE_PATH = os.path.join(self.tmp, "discovery_state.json")
        task_runner.REQUEST_PATH = os.path.join(self.tmp, "task_request.txt")
        task_runner.RESULT_PATH = os.path.join(self.tmp, "task_result.txt")
        forward.CHATINFO_SETTLE = 0
        forward.REMARK_SETTLE = 0
        discovery._reset_runtime_state()
        forward._STATE.clear()

        # 机器人侧 config.json（临时文件，模拟生产的三处键）
        self.config_file = os.path.join(self.tmp, "bot_config.json")
        self.bot_cfg = {
            "group": [ADMIN, "老友记们", "共建杭州美食地图🐶"],
            "group_api_map": {"老友记们": 2, "共建杭州美食地图🐶": 3},
            "group_prompt_map": {"老友记们": "肥肉", "共建杭州美食地图🐶": "肥肉"},
            "default_prompt": "肥肉",
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.bot_cfg, f, ensure_ascii=False, indent=4)
        self.memory_base = os.path.join(self.tmp, "memory")
        self.bot = mock.Mock()
        self.bot.wx = FakeWx()
        self.bot.config = FakeBotConfig(self.config_file, json.loads(json.dumps(self.bot_cfg)))
        self.bot.memory_manager = FakeMemoryManager(self.memory_base, "FeiRou_NCC")

        # 飞书 webhook 一律 mock
        self.sent = []
        self._wh = mock.patch("webhook_send.send_message",
                              side_effect=lambda t, c: (self.sent.append((t, c)) or (True, "ok")))
        self._wh.start()
        store.save({"admin_group": TEST_ADMIN,
                    "discovery": {"auto_tag_global": True, "auto_tag_listened": False,
                                  "digest_time": "09:00", "max_attempts": 3}})

    def tearDown(self):
        self._wh.stop()
        registry.DATA_DIR, registry.REGISTRY_PATH = self._orig["reg"]
        store.DATA_DIR, store.CONFIG_PATH = self._orig["store"]
        store._cache = None
        store._cache_mtime = None
        discovery.STATE_PATH = self._orig["state"]
        task_runner.REQUEST_PATH, task_runner.RESULT_PATH = self._orig["task"]
        forward.CHATINFO_SETTLE = self._orig["settle"]
        forward.REMARK_SETTLE = self._orig["sleep"]
        discovery._reset_runtime_state()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def _set_discovery(self, **kw):
        cfg = store.load()
        cfg.setdefault("discovery", {}).update(kw)
        store.save(cfg)

    def _seed(self, *names, applied=True):
        for n in names:
            registry.add_pending(n)
            if applied:
                registry.mark_remark_applied(n, n + DOG)

    def _global(self, name, attr="friend", with_remark_key=False, msgs=None):
        msgs = msgs if msgs is not None else [FakeMsg(attr=attr)]
        payload = {"chat_name": name, "chat_type": "group", "msg": msgs}
        if with_remark_key:
            payload["remark"] = name
        self.bot.wx.current = name
        return discovery.handle_global_group(self.bot, name, payload, msgs)

    def _file_cfg(self):
        with open(self.config_file, encoding="utf-8") as f:
            return json.load(f)


# ====================================================================== store 默认值

class StoreDefaultsTest(TaggingTestCase):
    def test_default_config_has_discovery_section(self):
        d = store.DEFAULT_CONFIG["discovery"]
        self.assertFalse(d["auto_tag_global"])
        self.assertFalse(d["auto_tag_listened"])
        self.assertEqual(d["digest_time"], "09:00")
        self.assertEqual(d["max_attempts"], 3)

    def test_settings_fall_back_when_section_missing(self):
        store.save({"admin_group": TEST_ADMIN})     # 没有 discovery 段
        s = discovery.settings()
        self.assertFalse(s["auto_tag_global"])
        self.assertEqual(s["max_attempts"], 3)
        # 部分覆盖：只给一个键，其它键仍是默认
        store.save({"admin_group": TEST_ADMIN, "discovery": {"auto_tag_global": True}})
        s = discovery.settings()
        self.assertTrue(s["auto_tag_global"])
        self.assertEqual(s["digest_time"], "09:00")


# ====================================================================== tagsync.post_tag

class PostTagTest(TaggingTestCase):
    def test_registers_and_marks_without_touching_config_for_unlistened_group(self):
        before = self._file_cfg()
        r = tagsync.post_tag(self.bot, "大理新群", "大理新群" + DOG, source="global")
        g = registry.load()["groups"]["大理新群"]
        self.assertTrue(g["remark_applied"])
        self.assertEqual(g["remark"], "大理新群" + DOG)
        self.assertEqual(g["status"], "pending")
        self.assertEqual(self._file_cfg(), before)          # 没碰 config.json
        self.assertFalse(r["config_synced"])
        self.assertEqual(len(self.sent), 1)
        title, content = self.sent[0]
        self.assertIn("大理新群", title)
        self.assertIn("加载配置", content)
        self.assertIn("index.json", content)

    def test_syncs_config_three_places_and_backs_up_for_listened_group(self):
        r = tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        self.assertTrue(r["config_synced"])
        cfg = self._file_cfg()
        self.assertNotIn("老友记们", cfg["group"])
        self.assertIn("老友记们" + DOG, cfg["group"])
        self.assertEqual(cfg["group"].index("老友记们" + DOG), 1)   # 位置不变
        # 两个 map：旧键保留、新键加上、值相同
        self.assertEqual(cfg["group_api_map"]["老友记们"], 2)
        self.assertEqual(cfg["group_api_map"]["老友记们" + DOG], 2)
        self.assertEqual(cfg["group_prompt_map"]["老友记们"], "肥肉")
        self.assertEqual(cfg["group_prompt_map"]["老友记们" + DOG], "肥肉")
        self.assertEqual(cfg["default_prompt"], "肥肉")            # 其它键原样
        # 备份
        baks = [f for f in os.listdir(self.tmp) if f.startswith("bot_config.json.bak-")]
        self.assertEqual(len(baks), 1)
        with open(os.path.join(self.tmp, baks[0]), encoding="utf-8") as f:
            self.assertEqual(json.load(f), self.bot_cfg)
        # 写盘格式：ensure_ascii=False + indent=4
        raw = open(self.config_file, encoding="utf-8").read()
        self.assertIn('    "group"', raw)
        self.assertNotIn("\\u", raw)
        # 内存里：运行中的子窗口 chat.who 可能仍是旧名，也可能已是新名 —— 两个都要认
        self.assertIn("老友记们", self.bot.config.group)
        self.assertIn("老友记们" + DOG, self.bot.config.group)
        self.assertEqual(self.bot.config.group_api_map["老友记们" + DOG], 2)
        self.assertEqual(self.bot.config.group_prompt_map["老友记们" + DOG], "肥肉")
        # 通知带两句提醒
        content = self.sent[0][1]
        self.assertIn("加载配置", content)
        self.assertIn("index.json", content)
        self.assertIn("老友记们" + DOG, content)

    def test_copies_memory_dir_to_new_name(self):
        src = os.path.join(self.memory_base, "FeiRou_NCC", "老友记们")
        os.makedirs(src)
        with open(os.path.join(src, "老友记们_memory.json"), "w", encoding="utf-8") as f:
            json.dump([{"role": "user", "content": "x"}], f, ensure_ascii=False)
        r = tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        dst = os.path.join(self.memory_base, "FeiRou_NCC", "老友记们" + DOG)
        self.assertTrue(os.path.isdir(dst))
        self.assertTrue(os.path.exists(os.path.join(dst, "老友记们" + DOG + "_memory.json")))
        self.assertTrue(os.path.isdir(src))                       # 旧目录不删
        self.assertTrue(r["memory_copied"])

    def test_memory_missing_is_not_an_error(self):
        r = tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        self.assertFalse(r["memory_copied"])
        self.assertEqual(len(self.sent), 1)

    def test_config_write_failure_is_reported_not_raised(self):
        self.bot.config.CONFIG_FILE = os.path.join(self.tmp, "nope", "config.json")
        r = tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        self.assertFalse(r["config_synced"])
        self.assertTrue(r["errors"])
        self.assertIn("config.json", self.sent[0][1])
        self.assertTrue(registry.load()["groups"]["老友记们"]["remark_applied"])

    def test_config_sync_is_idempotent(self):
        tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        tagsync.post_tag(self.bot, "老友记们", "老友记们" + DOG, source="listened")
        cfg = self._file_cfg()
        self.assertEqual(cfg["group"].count("老友记们" + DOG), 1)
        self.assertEqual(self.bot.config.group.count("老友记们" + DOG), 1)

    def test_notify_swallows_webhook_exception(self):
        self._wh.stop()
        with mock.patch("webhook_send.send_message", side_effect=RuntimeError("boom")):
            self.assertFalse(tagsync.notify("t", "c"))
        self._wh.start()


# ====================================================================== handle_global_group

class GlobalGroupTest(TaggingTestCase):
    def test_observe_mode_only_logs(self):
        self._set_discovery(auto_tag_global=False)
        with mock.patch.object(discovery, "log") as lg:
            self._global("新群X", with_remark_key=True)
        lines = [a[0][1] for a in lg.call_args_list if "[tagging-observe]" in a[0][1]]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("群=新群X", line)
        self.assertIn("attr=friend", line)
        self.assertIn("type=text", line)
        self.assertIn("有remark键=True", line)
        self.assertIn("判定=", line)
        self.assertNotIn("新群X", registry.load()["groups"])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(self.sent, [])

    def test_unnamed_group_is_skipped_silently(self):
        self._global("松爸、王伟")
        self.assertNotIn("松爸、王伟", registry.load()["groups"])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(self.sent, [])
        self.assertIn("松爸、王伟", discovery._UNNAMED_SEEN)

    def test_admin_groups_never_tagged(self):
        for name in (ADMIN, TEST_ADMIN):
            self._global(name)
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(registry.load()["groups"], {})
        self.assertEqual(self.sent, [])

    def test_known_tagged_group_only_touches_last_seen(self):
        self._seed("大理A群")
        self._global("大理A群" + DOG)
        g = registry.load()["groups"]["大理A群"]
        self.assertTrue(g["last_seen"])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(self.sent, [])

    def test_tagged_but_unregistered_group_is_registered_as_applied(self):
        self._global("野群" + DOG)
        g = registry.load()["groups"]["野群"]
        self.assertTrue(g["remark_applied"])
        self.assertEqual(g["status"], "pending")
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(len(self.sent), 1)
        self._global("野群" + DOG)
        self.assertEqual(len(self.sent), 1)          # 第二次不再提醒

    def test_too_long_name_goes_pending_and_notifies_once(self):
        name = _long_name()
        self._global(name)
        g = registry.load()["groups"][name]
        self.assertFalse(g["remark_applied"])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("48", self.sent[0][1])
        self._global(name)
        self.assertEqual(len(self.sent), 1)

    def test_garbage_remark_goes_pending_and_notifies_once(self):
        name = "A群" + DOG + "A群" + DOG
        self._global(name)
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("人工", self.sent[0][1])
        self._global(name)
        self.assertEqual(len(self.sent), 1)

    def test_fix_apply_tags_in_place_without_chatwith(self):
        self._global("新群X")
        self.assertEqual(self.bot.wx.chatted, [])                 # 绝不 ChatWith
        self.assertEqual(self.bot.wx.remark_calls, [("新群X", "新群X" + DOG)])
        g = registry.load()["groups"]["新群X"]
        self.assertTrue(g["remark_applied"])
        self.assertEqual(g["remark"], "新群X" + DOG)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("新群X", self.sent[0][0])
        # 同一群下一条消息（显示名已是备注）不再动
        self._global("新群X" + DOG)
        self.assertEqual(len(self.bot.wx.remark_calls), 1)
        self.assertEqual(len(self.sent), 1)

    def test_window_mismatch_records_attempt_and_alerts_on_third(self):
        self.bot.wx.current = "别的群"
        payload = {"chat_name": "新群Y", "chat_type": "group", "msg": [FakeMsg()]}
        for i in range(1, 4):
            discovery.handle_global_group(self.bot, "新群Y", payload, payload["msg"])
            g = registry.load()["groups"]["新群Y"]
            self.assertFalse(g["remark_applied"])
            self.assertEqual(g["tag_attempts"], i)
            self.assertIn("别的群", g["tag_last_error"])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(len(self.sent), 1)                      # 第 3 次才叫人
        self.assertIn("人工", self.sent[0][1])
        # 第 4 次：不再试、不再叫
        discovery.handle_global_group(self.bot, "新群Y", payload, payload["msg"])
        self.assertEqual(registry.load()["groups"]["新群Y"]["tag_attempts"], 3)
        self.assertEqual(len(self.sent), 1)

    def test_wrong_chat_type_is_refused(self):
        self.bot.wx.chat_types["新群Z"] = "friend"
        self._global("新群Z")
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertEqual(registry.load()["groups"]["新群Z"]["tag_attempts"], 1)

    def test_verify_failure_counts_as_attempt(self):
        self.bot.wx.remark_takes_effect = False
        self._global("新群W")
        self.assertEqual(len(self.bot.wx.remark_calls), 1)
        g = registry.load()["groups"]["新群W"]
        self.assertFalse(g["remark_applied"])
        self.assertEqual(g["tag_attempts"], 1)

    def test_listened_group_not_tagged_unless_listened_switch_on(self):
        self._global("老友记们")
        self.assertEqual(self.bot.wx.remark_calls, [])
        g = registry.load()["groups"]["老友记们"]
        self.assertFalse(g["remark_applied"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("监听", self.sent[0][1])
        self._global("老友记们")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._file_cfg(), self.bot_cfg)

    def test_listened_group_tagged_and_config_synced_when_enabled(self):
        self._set_discovery(auto_tag_listened=True)
        self._global("老友记们")
        self.assertEqual(self.bot.wx.remark_calls, [("老友记们", "老友记们" + DOG)])
        self.assertTrue(registry.load()["groups"]["老友记们"]["remark_applied"])
        cfg = self._file_cfg()
        self.assertIn("老友记们" + DOG, cfg["group"])
        self.assertNotIn("老友记们", cfg["group"])
        self.assertEqual(cfg["group_api_map"]["老友记们" + DOG], 2)
        self.assertEqual(len(self.sent), 1)

    def test_system_messages_also_trigger(self):
        self._global("新群S", attr="system", msgs=[FakeMsg(attr="system", mtype="system",
                                                            content='"张三"邀请你加入了群聊')])
        self.assertEqual(self.bot.wx.remark_calls, [("新群S", "新群S" + DOG)])

    def test_plugin_errors_do_not_raise(self):
        with mock.patch.object(registry, "load", side_effect=RuntimeError("坏了")):
            discovery.handle_global_group(self.bot, "新群E",
                                          {"chat_name": "新群E", "chat_type": "group", "msg": []}, [])


# ====================================================================== handle_discovery（friend 分支）

class ListenedDiscoveryTest(TaggingTestCase):
    def _friend(self, who, chat_type="group"):
        cfg = store.load()
        discovery.handle_discovery(self.bot, FakeChat(who, chat_type), FakeMsg(), cfg)

    def test_known_group_touches_but_never_chatwith(self):
        self._seed("大理A群", applied=False)
        self._friend("大理A群")
        self.assertEqual(self.bot.wx.chatted, [])
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertTrue(registry.load()["groups"]["大理A群"]["last_seen"])
        self.assertEqual(self.sent, [])                          # 即时提醒已删
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_new_group_registers_pending_and_notifies_once(self):
        self._friend("老友记们")
        g = registry.load()["groups"]["老友记们"]
        self.assertEqual(g["status"], "pending")
        self.assertFalse(g["remark_applied"])
        self.assertEqual(self.bot.wx.chatted, [])
        self.assertEqual(len(self.sent), 1)
        self._friend("老友记们")
        self.assertEqual(len(self.sent), 1)

    def test_enqueues_fix_remark_when_listened_switch_on(self):
        self._set_discovery(auto_tag_listened=True)
        self._friend("老友记们")
        with open(task_runner.REQUEST_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "修备注 老友记们")
        self.assertEqual(self.bot.wx.chatted, [])
        os.remove(task_runner.REQUEST_PATH)
        self._friend("老友记们")                                  # 冷却期内不重复排队
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_does_not_overwrite_pending_request(self):
        self._set_discovery(auto_tag_listened=True)
        with open(task_runner.REQUEST_PATH, "w", encoding="utf-8") as f:
            f.write("检查群组 全部")
        self._friend("老友记们")
        with open(task_runner.REQUEST_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read(), "检查群组 全部")

    def test_no_enqueue_when_switch_off_or_not_listened_or_tagged(self):
        self._friend("老友记们")                                  # 开关关
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))
        self._set_discovery(auto_tag_listened=True)
        self._friend("不在监听的群")                              # 不在 config.group
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))
        self._friend("共建杭州美食地图🐶")                         # 已带🐶
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_no_enqueue_after_max_attempts(self):
        self._set_discovery(auto_tag_listened=True)
        registry.add_pending("老友记们")
        for _ in range(3):
            registry.record_tag_attempt("老友记们", "x")
        self._friend("老友记们")
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_ignores_admin_and_private(self):
        self._friend(ADMIN)
        self._friend(TEST_ADMIN)
        self._friend("私聊对象", chat_type="friend")
        self.assertEqual(registry.load()["groups"], {})
        self.assertEqual(self.sent, [])


# ====================================================================== 每日汇总

class DigestTest(TaggingTestCase):
    def test_digest_lists_untagged_and_records_date(self):
        self._seed("已打群")
        registry.add_pending("没打群")
        registry.add_pending("怪备注群")
        registry.mark_remark_applied("怪备注群", "怪备注群")         # applied 但备注里没🐶
        registry.add_pending(TEST_ADMIN)                           # 管理群不进汇总
        discovery.digest_tick(self.bot)
        self.assertEqual(len(self.sent), 1)
        title, content = self.sent[0]
        self.assertIn("没打群", content)
        self.assertIn("怪备注群", content)
        self.assertNotIn("已打群", content)
        self.assertNotIn(TEST_ADMIN, content)
        with open(discovery.STATE_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["digest_last_date"], date.today().isoformat())
        discovery.digest_tick(self.bot)
        self.assertEqual(len(self.sent), 1)                       # 同一天不重发

    def test_digest_empty_sends_nothing_but_records(self):
        self._seed("已打群")
        discovery.digest_tick(self.bot)
        self.assertEqual(self.sent, [])
        self.assertTrue(os.path.exists(discovery.STATE_PATH))

    def test_register_digest_uses_configured_time(self):
        self._set_discovery(digest_time="08:30")
        sch = FakeSchedule()
        discovery.register_digest(self.bot, sch)
        self.assertEqual([j.at_time for j in sch.jobs], ["08:30"])
        self.assertIn("ncc_tag_digest", sch.cleared)
        self._set_discovery(digest_time="乱写")
        sch = FakeSchedule()
        discovery.register_digest(self.bot, sch)
        self.assertEqual([j.at_time for j in sch.jobs], ["09:00"])

    def test_task_runner_register_also_registers_digest(self):
        sch = FakeSchedule()
        task_runner.register(self.bot, sch)
        ats = [j.at_time for j in sch.jobs if j.at_time]
        self.assertEqual(ats, ["09:00"])
        self.assertTrue(self.bot._ncc_task_runner_enabled)


# ====================================================================== 「修备注 <群名>」

class FixOneRemarkTest(TaggingTestCase):
    def _cmd(self, text):
        chat = FakeChat(TEST_ADMIN)
        handled = forward._try_direct_command(self.bot, chat, store.load(), "大松", text)
        return handled, chat.sent

    def test_single_group_switches_confirms_tags_and_syncs(self):
        registry.add_pending("老友记们")
        handled, sent = self._cmd("修备注 老友记们")
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted, ["老友记们"])
        self.assertEqual(self.bot.wx.remark_calls, [("老友记们", "老友记们" + DOG)])
        self.assertTrue(registry.load()["groups"]["老友记们"]["remark_applied"])
        self.assertIn("老友记们" + DOG, self._file_cfg()["group"])   # 监听中的群 → 同步 config
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(any("老友记们" + DOG in (m or "") for m in sent))

    def test_single_group_switch_failure_reports(self):
        self.bot.wx.fail_names.add("幽灵群")
        handled, sent = self._cmd("修备注 幽灵群")
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertTrue(any("切" in (m or "") for m in sent))
        self.assertEqual(self.sent, [])

    def test_single_group_already_tagged_reports_ok(self):
        self.bot.wx.remarks["大理A群"] = "大理A群" + DOG
        self._seed("大理A群")
        handled, sent = self._cmd("修备注 大理A群")
        self.assertEqual(self.bot.wx.remark_calls, [])
        self.assertTrue(any("已" in (m or "") for m in sent))

    def test_single_group_admin_refused(self):
        handled, sent = self._cmd("修备注 " + ADMIN)
        self.assertTrue(handled)
        self.assertEqual(self.bot.wx.chatted, [])
        self.assertEqual(self.bot.wx.remark_calls, [])

    def test_preview_and_all_still_work_as_before(self):
        self.bot.wx.GetAllRecentGroups = lambda: []
        handled, sent = self._cmd("修备注 预览")
        self.assertTrue(handled)
        self.assertTrue(any("没扫到" in (m or "") for m in sent))


# ====================================================================== wxbot_core hook 位置

class CoreHookTest(unittest.TestCase):
    def test_hook_sits_between_blacklist_filter_and_msgs_loop(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "wxbot_core.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("def get_next_new_message():")
        end = src.index("get_next_new_message()", start + 30)
        body = src[start:end]
        self.assertEqual(body.count("handle_global_group"), 1)
        i_black = body.index("为黑名单用户，跳过处理")
        i_hook = body.index("handle_global_group")
        i_msgs = body.index("if msgs:")
        self.assertLess(i_black, i_hook)
        self.assertLess(i_hook, i_msgs)
        self.assertIn("if chat_type == 'group':", body[i_black:i_hook])
        self.assertEqual(src.count("handle_global_group"), 1)     # 全文件只此一处


if __name__ == "__main__":
    unittest.main(verbosity=1)
