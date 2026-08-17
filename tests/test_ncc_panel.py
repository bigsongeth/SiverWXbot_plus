# -*- coding: utf-8 -*-
"""面板 /ncc_community 单测（PANEL_SPEC.md §6）：registry CRUD + panel 状态/操作
+ 去 Notion 化迁移脚本。纯 Python，不碰 flask、不碰微信，mac 上直接跑：

    PYTHONPATH=. python3 tests/test_ncc_panel.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

# 单测的日志不许写进 panel_logs（生产日志）——必须在导入插件【之前】设。
os.environ.setdefault("NCC_LOG_SILENT", "1")

from plugins.ncc_community import registry, store, panel, migrate_notion_off, task_runner


class PanelTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ncc_panel_")
        self._r = (registry.DATA_DIR, registry.REGISTRY_PATH)
        registry.DATA_DIR = self.tmp
        registry.REGISTRY_PATH = os.path.join(self.tmp, "registry.json")
        self._s = (store.DATA_DIR, store.CONFIG_PATH)
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None
        # 体检任务的请求/结果文件也指到临时目录，别碰生产机上的真文件
        self._t = (task_runner.DATA_DIR, task_runner.REQUEST_PATH, task_runner.RESULT_PATH)
        task_runner.DATA_DIR = self.tmp
        task_runner.REQUEST_PATH = os.path.join(self.tmp, "task_request.txt")
        task_runner.RESULT_PATH = os.path.join(self.tmp, "task_result.txt")

    def tearDown(self):
        (task_runner.DATA_DIR, task_runner.REQUEST_PATH,
         task_runner.RESULT_PATH) = self._t
        registry.DATA_DIR, registry.REGISTRY_PATH = self._r
        store.DATA_DIR, store.CONFIG_PATH = self._s
        store._cache = None
        store._cache_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self):
        """两个分组、三个群、一条关键词。"""
        registry.set_grouping("大理群", number=4, forward_enabled=True)
        registry.set_grouping("黄山群", number=6, forward_enabled=True)
        registry.add_group("大理A群", groupings=["大理群"], allow_forward=True)
        registry.add_group("大理B群", groupings=["大理群"], allow_forward=True)
        registry.add_group("黄山A群", groupings=["黄山群"])
        registry.set_invite_keyword("大理", "大理A群")


# ---------------------------------------------------------------- registry CRUD

class RegistryCrudTests(PanelTestBase):
    def test_add_group_gets_gid_and_defaults(self):
        g = registry.add_group("新群")
        self.assertEqual(len(g["gid"]), 8)
        self.assertFalse(g["allow_forward"])       # 未归类前默认不参与转发，安全
        self.assertEqual(g["status"], "active")
        self.assertEqual(g["remark"], "新群" + registry.DOG)

    def test_add_group_rejects_duplicate_and_blank(self):
        registry.add_group("新群")
        with self.assertRaises(ValueError):
            registry.add_group("新群")
        with self.assertRaises(ValueError):
            registry.add_group("   ")

    def test_gids_are_unique(self):
        for i in range(30):
            registry.add_group(f"群{i}")
        gids = [g["gid"] for g in registry.load()["groups"].values()]
        self.assertEqual(len(gids), len(set(gids)))

    def test_set_group_fields(self):
        self.seed()
        registry.set_group_fields("黄山A群", allow_forward=True, allow_speak=True,
                                  welcome_url="https://x", groupings=["黄山群", "大理群"])
        g = registry.load()["groups"]["黄山A群"]
        self.assertTrue(g["allow_forward"] and g["allow_speak"])
        self.assertEqual(g["welcome_url"], "https://x")
        self.assertEqual(g["groupings"], ["黄山群", "大理群"])

    def test_set_group_fields_rejects_unknown_grouping(self):
        """分组名拼错一个字就会造出一个谁也发不到的死分组，必须挡住。"""
        self.seed()
        with self.assertRaises(ValueError):
            registry.set_group_fields("大理A群", groupings=["大理裙"])
        with self.assertRaises(ValueError):
            registry.set_group_fields("大理A群", 允许转发=True)

    def test_set_group_fields_dedups_groupings(self):
        self.seed()
        registry.set_group_fields("大理A群", groupings=["大理群", " 大理群 ", ""])
        self.assertEqual(registry.load()["groups"]["大理A群"]["groupings"], ["大理群"])

    # ---------- 改名 ----------

    def test_rename_keeps_gid_and_applied_remark(self):
        """打过🐶备注的群改名后仍按【微信里那个真实备注】寻址——
        不继承的话 remark 变成「新名🐶」而微信里还是「老名🐶」，寻址必然落空。"""
        self.seed()
        registry.mark_remark_applied("大理A群", "大理A群🐶")
        gid = registry.load()["groups"]["大理A群"]["gid"]
        registry.rename_group("大理A群", "大理A群改名了")
        data = registry.load()
        self.assertNotIn("大理A群", data["groups"])
        g = data["groups"]["大理A群改名了"]
        self.assertEqual(g["gid"], gid)
        self.assertEqual(g["remark"], "大理A群🐶")        # 继承真实备注
        self.assertIn("大理A群🐶", registry.address_candidates(g))

    def test_rename_without_remark_follows_new_name(self):
        self.seed()
        registry.rename_group("大理B群", "大理B群新")
        g = registry.load()["groups"]["大理B群新"]
        self.assertEqual(g["remark"], "大理B群新" + registry.DOG)

    def test_rename_drops_stale_addressing_hit(self):
        """上次靠老群名命中的，改名后那个串已经不存在了，别再拿它当首选。"""
        self.seed()
        registry.mark_addressing("大理A群", "大理A群")
        registry.rename_group("大理A群", "大理A群新")
        self.assertIsNone(registry.load()["groups"]["大理A群新"]["addressing_hit"])

    def test_rename_migrates_invite_keywords(self):
        """指向老名的拉群关键词要跟着走，否则关键词当场失效。"""
        self.seed()
        registry.rename_group("大理A群", "大理A群新")
        self.assertEqual(registry.invite_map(registry.load())["大理"], "大理A群新")

    def test_rename_rejects_collision(self):
        self.seed()
        with self.assertRaises(ValueError):
            registry.rename_group("大理A群", "大理B群")

    # ---------- 归类 / 恢复 / 删除 ----------

    def test_classify_pending(self):
        self.seed()
        registry.add_pending("刚发现的群")
        self.assertEqual(registry.load()["groups"]["刚发现的群"]["status"], "pending")
        registry.classify_pending("刚发现的群", groupings=["大理群"], allow_forward=True)
        g = registry.load()["groups"]["刚发现的群"]
        self.assertEqual(g["status"], "active")
        self.assertTrue(g["allow_forward"])
        self.assertIn("刚发现的群", [s["name"] for s in registry.forward_specs(registry.load())])

    def test_restore_reachable(self):
        self.seed()
        registry.mark_unreachable("大理A群")
        self.assertEqual(registry.load()["groups"]["大理A群"]["status"], "unreachable")
        registry.restore_reachable("大理A群")
        g = registry.load()["groups"]["大理A群"]
        self.assertEqual(g["status"], "active")
        self.assertTrue(g["allow_forward"])
        self.assertIsNone(g["addressing_hit"])     # 上次命中的串多半已失效

    def test_delete_group_cascades_to_keywords(self):
        self.seed()
        registry.delete_group("大理A群")
        self.assertNotIn("大理A群", registry.load()["groups"])
        self.assertNotIn("大理", registry.load()["invite_keywords"])

    # ---------- 分组 ----------

    def test_grouping_number_must_be_unique(self):
        self.seed()
        with self.assertRaises(ValueError):
            registry.set_grouping("新分组", number=4)     # 4 已被大理群占用

    def test_grouping_number_one_reserved(self):
        with self.assertRaises(ValueError):
            registry.set_grouping("新分组", number=1)     # 1 = 所有群聊

    def test_grouping_number_can_be_blank(self):
        registry.set_grouping("无编号组", number="")
        self.assertIsNone(registry.load()["groupings"]["无编号组"]["number"])

    def test_rename_grouping_updates_group_refs(self):
        self.seed()
        registry.rename_grouping("大理群", "大理片区")
        data = registry.load()
        self.assertNotIn("大理群", data["groupings"])
        self.assertEqual(data["groups"]["大理A群"]["groupings"], ["大理片区"])

    def test_delete_grouping_detaches_groups(self):
        self.seed()
        registry.delete_grouping("大理群")
        data = registry.load()
        self.assertEqual(data["groups"]["大理A群"]["groupings"], [])
        # 分组没了，转发选择菜单里也就没有它了（黄山群没有允许转发的群，本来就不出现）
        self.assertEqual(registry.forward_groupings_detailed(data), [])

    # ---------- 拉群关键词 ----------

    def test_invite_keyword_requires_known_group(self):
        self.seed()
        with self.assertRaises(ValueError):
            registry.set_invite_keyword("张三", "根本没有这个群")

    def test_invite_map_skips_disabled(self):
        self.seed()
        registry.set_invite_keyword("大理", "大理A群", enabled=False)
        self.assertNotIn("大理", registry.invite_map(registry.load()))
        self.assertIn("大理", registry.load()["invite_keywords"])   # 配置仍在

    def test_invite_entry_reads_legacy_string_form(self):
        """老格式 {关键词: 群名} 也要读得动（迁移前/回滚后的数据）。"""
        data = registry.load()
        data["invite_keywords"] = {"老词": "老群"}
        registry.save(data)
        self.assertEqual(registry.invite_map(registry.load()), {"老词": "老群"})


# ---------------------------------------------------------------- panel 层

class PanelStateTests(PanelTestBase):
    def test_state_shape_and_stats(self):
        self.seed()
        registry.add_pending("待归类群")
        registry.mark_unreachable("大理B群")
        st = panel.state()
        self.assertEqual(st["stats"]["groups"], 4)
        self.assertEqual(st["stats"]["forwardable"], 1)     # B 群被标不可达后不再转发
        self.assertEqual(st["stats"]["unreachable"], 1)
        self.assertEqual(st["stats"]["pending"], 1)
        self.assertEqual(st["stats"]["keywords_on"], 1)
        self.assertEqual([g["name"] for g in st["pending"]], ["待归类群"])
        self.assertNotIn("待归类群", [g["name"] for g in st["groups"]])

    def test_state_groupings_sorted_by_number(self):
        registry.set_grouping("丙", number=None)
        registry.set_grouping("乙", number=9)
        registry.set_grouping("甲", number=2)
        names = [g["name"] for g in panel.state()["groupings"]]
        self.assertEqual(names, ["甲", "乙", "丙"])        # 无编号的排最后

    def test_state_backfills_missing_gid(self):
        """迁移前就存在、迁移后又新发现的群，读页面时顺手补上 gid。"""
        data = registry.load()
        data["groups"]["老群"] = {"name": "老群", "allow_forward": True, "groupings": []}
        registry.save(data)
        self.assertTrue(panel.state()["groups"][0]["gid"])
        self.assertTrue(registry.load()["groups"]["老群"]["gid"])   # 已落盘

    def test_state_flags_stale_groupings(self):
        """群身上挂着已被删掉的分组名（历史脏数据），界面上要标出来。"""
        self.seed()
        data = registry.load()
        data["groups"]["大理A群"]["groupings"] = ["大理群", "早没了的组"]
        registry.save(data)
        g = next(x for x in panel.state()["groups"] if x["name"] == "大理A群")
        self.assertEqual(g["stale_groupings"], ["早没了的组"])

    def test_state_marks_invalid_keyword_target(self):
        self.seed()
        data = registry.load()
        data["groups"].pop("大理A群")        # 绕过 CRUD 造出悬空关键词
        registry.save(data)
        kw = panel.state()["invite_keywords"][0]
        self.assertFalse(kw["valid"])

    def test_panel_url_from_config(self):
        cfg = store.load()
        cfg["panel_url"] = "http://127.0.0.1:10002/ncc_community"
        store.save(cfg)
        self.assertEqual(panel.panel_url(), "http://127.0.0.1:10002/ncc_community")


class PanelActionTests(PanelTestBase):
    def test_每个操作都能跑通(self):
        self.seed()
        cases = [
            ("group.add", {"name": "面板新增群"}),
            ("group.save", {"name": "面板新增群", "groupings": ["大理群"],
                            "allow_forward": True, "allow_speak": True,
                            "welcome_url": "https://w"}),
            ("group.rename", {"name": "面板新增群", "new_name": "面板改名群"}),
            ("grouping.save", {"name": "新分组", "number": 8, "forward_enabled": True}),
            ("grouping.save", {"old_name": "新分组", "name": "新分组B", "number": 8,
                               "forward_enabled": False}),
            ("invite.save", {"keyword": "面板词", "group": "面板改名群", "enabled": True}),
            ("invite.save", {"old_keyword": "面板词", "keyword": "面板词2",
                             "group": "面板改名群", "enabled": False}),
            ("invite.delete", {"keyword": "面板词2"}),
            ("grouping.delete", {"name": "新分组B"}),
            ("group.delete", {"name": "面板改名群"}),
        ]
        for op, payload in cases:
            self.assertTrue(panel.apply(op, payload), op)
        data = registry.load()
        self.assertNotIn("面板改名群", data["groups"])
        self.assertNotIn("新分组B", data["groupings"])
        self.assertNotIn("面板词2", data["invite_keywords"])

    def test_classify_via_panel(self):
        self.seed()
        registry.add_pending("新来的群")
        panel.apply("group.classify", {"name": "新来的群", "groupings": ["大理群"],
                                       "allow_forward": True})
        self.assertEqual(registry.load()["groups"]["新来的群"]["status"], "active")

    def test_restore_via_panel(self):
        self.seed()
        registry.mark_unreachable("大理A群")
        panel.apply("group.restore", {"name": "大理A群"})
        self.assertEqual(registry.load()["groups"]["大理A群"]["status"], "active")

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            panel.apply("group.drop_database", {})

    def test_invalid_payload_raises_not_swallowed(self):
        """静默吞掉的保存会让人以为改成功了——必须抛。"""
        self.seed()
        with self.assertRaises(ValueError):
            panel.apply("group.save", {"name": "不存在的群", "groupings": []})
        with self.assertRaises(ValueError):
            panel.apply("invite.save", {"keyword": "x", "group": "不存在的群"})
        with self.assertRaises(ValueError):
            panel.apply("group.add", {"name": ""})


# ---------------------------------------------------------------- 迁移脚本

class MigrationTests(PanelTestBase):
    def _legacy_registry(self):
        """迁移前的样子：没有 gid，关键词是纯字符串。"""
        registry.save({
            "synced_at": "2026-08-01T00:00:00",
            "groupings": {"大理群": {"number": 4, "forward_enabled": True}},
            "groups": {
                "大理A群": {"name": "大理A群", "remark": "大理A群🐶", "remark_applied": True,
                          "notion_page_id": "pg1", "allow_forward": True, "allow_speak": False,
                          "welcome_url": "", "groupings": ["大理群"], "status": "active"},
                "大理B群": {"name": "大理B群", "remark": "大理B群🐶", "remark_applied": False,
                          "notion_page_id": "pg2", "allow_forward": True, "allow_speak": False,
                          "welcome_url": "", "groupings": ["大理群"], "status": "active"},
            },
            "invite_keywords": {"大理": "大理A群"},
        })

    def test_migration_backfills_gid_and_upgrades_keywords(self):
        self._legacy_registry()
        cfg = store.load()
        cfg["invite"] = {"keywords": {"本地词": "大理B群"}, "daily_limit": 3}
        store.save(cfg)

        stat = migrate_notion_off.run()
        self.assertEqual(stat["gids"], 2)
        self.assertEqual(stat["upgraded"], 1)
        self.assertEqual(stat["merged"], 1)

        data = registry.load()
        self.assertTrue(all(g["gid"] for g in data["groups"].values()))
        self.assertEqual(registry.invite_map(data),
                         {"大理": "大理A群", "本地词": "大理B群"})
        self.assertTrue(os.path.exists(stat["backup"]))

    def test_migration_is_idempotent(self):
        self._legacy_registry()
        first = migrate_notion_off.run()
        before = json.load(open(registry.REGISTRY_PATH, encoding="utf-8"))
        second = migrate_notion_off.run()
        after = json.load(open(registry.REGISTRY_PATH, encoding="utf-8"))
        self.assertEqual((second["gids"], second["upgraded"], second["merged"]), (0, 0, 0))
        self.assertEqual(before["groups"], after["groups"])
        self.assertEqual(before["invite_keywords"], after["invite_keywords"])
        self.assertTrue(first["backup"])

    def test_migration_dry_run_changes_nothing(self):
        self._legacy_registry()
        before = json.load(open(registry.REGISTRY_PATH, encoding="utf-8"))
        stat = migrate_notion_off.run(dry=True)
        after = json.load(open(registry.REGISTRY_PATH, encoding="utf-8"))
        self.assertEqual(stat["gids"], 2)      # 报告"会补 2 个"
        self.assertEqual(before, after)        # 但一个字没改
        self.assertEqual(stat["backup"], "")

    def test_migration_reports_dangling_keywords(self):
        self._legacy_registry()
        cfg = store.load()
        cfg["invite"] = {"keywords": {}, "daily_limit": 3}   # 默认配置自带的样例词会干扰
        store.save(cfg)
        data = registry.load()
        data["invite_keywords"]["野词"] = "早没了的群"
        registry.save(data)
        self.assertEqual(migrate_notion_off.run(dry=True)["dangling"], ["野词"])

    def test_local_override_wins_on_conflict(self):
        """config.json 的 invite.keywords 一直是同名优先的覆盖层，合并时保持这个语义。"""
        self._legacy_registry()
        cfg = store.load()
        cfg["invite"] = {"keywords": {"大理": "大理B群"}, "daily_limit": 3}
        store.save(cfg)
        migrate_notion_off.run()
        self.assertEqual(registry.invite_map(registry.load())["大理"], "大理B群")


class HealthTaskTests(PanelTestBase):
    """体检指令搬到面板（2026-08-15）：按钮 → 请求文件 → bot 取走 → 结果回读。"""

    def test_catalog_hides_real_command_string(self):
        # 前端只拿到 id，指令串在服务端查表 —— 前端拼不出没授权的指令
        cat = panel.task_catalog()
        self.assertTrue(cat)
        for c in cat:
            self.assertNotIn("cmd", c)
            self.assertTrue(c["id"] and c["label"] and c["hint"])
        self.assertTrue(any(c["danger"] for c in cat))      # 修备注要标危险

    def test_run_task_writes_command_without_bom(self):
        # ★ BOM 是实测踩过的坑：PowerShell 的 Set-Content -Encoding UTF8 带 BOM，
        # bot 那边读成「\ufeff检查群组 全部」，指令表怎么也匹配不上，还看不出差别
        msg = panel.run_task("check_groups")
        self.assertIn("检查群组 全部", msg)
        raw = open(task_runner.REQUEST_PATH, "rb").read()
        self.assertEqual(raw, "检查群组 全部".encode("utf-8"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_run_task_rejects_unknown_id(self):
        with self.assertRaises(ValueError):
            panel.run_task("rm -rf /")
        self.assertFalse(os.path.exists(task_runner.REQUEST_PATH))

    def test_run_task_rejects_while_previous_pending(self):
        panel.run_task("scan_groups")
        with self.assertRaises(ValueError):          # 上一条还没被取走
            panel.run_task("check_groups")

    def test_run_task_rejects_while_running(self):
        # 结果文件有内容、又没有结束行 = 正在跑。两个任务同时跑会互相抢微信窗口。
        open(task_runner.RESULT_PATH, "w", encoding="utf-8").write("=== 后台任务「扫群」开始 ===\n")
        with self.assertRaises(ValueError):
            panel.run_task("check_groups")

    def test_task_status_running_then_done(self):
        self.assertFalse(panel.task_status()["running"])       # 没结果文件 = 没在跑
        with open(task_runner.RESULT_PATH, "w", encoding="utf-8") as f:
            f.write("=== 后台任务「检查群组 全部」开始 ===\n\n[18:07] 开始检查 87 个群…\n")
        st = panel.task_status()
        self.assertTrue(st["running"])
        self.assertIn("开始检查 87 个群", st["result"])
        with open(task_runner.RESULT_PATH, "a", encoding="utf-8") as f:
            f.write("\n=== 任务结束，耗时 412.0 秒 ===\n")
        self.assertFalse(panel.task_status()["running"])

    def test_apply_routes_task_run(self):
        msg = panel.apply("task.run", {"task": "scan_groups"})
        self.assertIn("扫群", msg)
        self.assertEqual(open(task_runner.REQUEST_PATH, encoding="utf-8").read(), "扫群")


if __name__ == "__main__":
    unittest.main()
