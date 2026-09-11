# -*- coding: utf-8 -*-
"""回复里的固定链接：收录后必带「高德里打开」、收录/推荐结尾必带「查看全部」。

起因（2026-09-11）：hzfood 的 recommend_place 每次都返回「高德里打开：<短链>」，技能里也写了要带上，
可 20:03 那条「收好了：狮山路163号的遵义羊肉粉…」就是没带 —— 提示词管不住，交给代码。
"""
from __future__ import annotations
import unittest
from brain.gateway import reply_links as RL

FOOTER = "查看全部小众点评：https://surl.amap.com/4bvd5QZ4HY"
RULES = {"hz-food-map": {
    "link_tools": ["recommend_place", "recommend_from_link"],
    "footer_tools": ["recommend_place", "recommend_from_link", "suggest"],
    "footer": FOOTER,
}}
LINK = "https://food.bigsong.site/p/B0L1M6FCKY"


def _call(cid, name):
    return {"type": "tool/call", "data": {"callId": cid, "name": f"mcp__hzfood__{name}", "arguments": "{}"}}


def _result(cid, text):
    return {"type": "tool/result",
            "data": {"message": {"source": {"callId": cid}, "content": [{"type": "text", "text": text}]}}}


RECORDED = [_call("1", "register"), _result("1", '{"agent_id":"a1"}'),
            _call("2", "search_place"), _result("2", '{"poiid":"B0L1M6FCKY"}'),
            _call("3", "recommend_place"),
            _result("3", f"已收录「遵义羊肉粉」，推荐人 松爸，进「吃」。描述：\n正宗贵州菜\n高德里打开：{LINK}")]


class LinkTest(unittest.TestCase):
    def test_adds_missing_store_link_and_footer(self):
        out, added = RL.ensure_links(["收好了：狮山路163号的遵义羊肉粉，进「吃」文件夹 🐾"], RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(len(out), 1)
        self.assertIn(f"高德里打开：{LINK}", out[0])
        self.assertTrue(out[0].endswith(FOOTER))
        self.assertEqual(set(added), {"link", "footer"})

    def test_does_not_duplicate_link_model_already_wrote(self):
        out, added = RL.ensure_links([f"收好了 {LINK}"], RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(out[0].count(LINK), 1)
        self.assertNotIn("link", added)

    def test_does_not_duplicate_footer(self):
        out, added = RL.ensure_links([f"收好了 {LINK}\n{FOOTER}"], RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(out[0].count("4bvd5QZ4HY"), 1)
        self.assertEqual(added, [])

    def test_footer_dedup_by_url_even_if_model_worded_it_differently(self):
        out, _ = RL.ensure_links(["整张地图在这 https://surl.amap.com/4bvd5QZ4HY"], RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(out[0].count("4bvd5QZ4HY"), 1)

    def test_appends_to_last_bubble_only(self):
        out, _ = RL.ensure_links(["第一条", "第二条"], RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(out[0], "第一条")
        self.assertIn(LINK, out[1]); self.assertIn(FOOTER, out[1])

    def test_suggest_gets_footer_but_no_store_links(self):
        ev = [_call("1", "suggest"), _result("1", '[{"name":"宽马记","amap":"https://food.bigsong.site/p/X"}]')]
        out, added = RL.ensure_links(["宽马记·吊龙绝了 https://food.bigsong.site/p/X"], ev, ["hz-food-map"], RULES)
        self.assertEqual(added, ["footer"])
        self.assertTrue(out[0].endswith(FOOTER))

    def test_plain_chat_turn_untouched(self):
        out, added = RL.ensure_links(["信心爆棚 🐶"], [], ["hz-food-map"], RULES)
        self.assertEqual(out, ["信心爆棚 🐶"]); self.assertEqual(added, [])

    def test_search_only_turn_untouched(self):
        # 只搜没收（校验不过、回问「是这家吗」）—— 不能挂链接，也不挂结尾
        ev = [_call("1", "search_place"), _result("1", '{"poiid":"B0X"}')]
        out, added = RL.ensure_links(["我搜到的是 XX，是这家吗？"], ev, ["hz-food-map"], RULES)
        self.assertEqual(added, [])

    def test_other_group_untouched(self):
        out, added = RL.ensure_links(["收好了"], RECORDED, ["ai-geek-cold"], RULES)
        self.assertEqual(out, ["收好了"]); self.assertEqual(added, [])

    def test_failed_recommend_has_no_link_to_add(self):
        ev = [_call("1", "recommend_place"), _result("1", "写入失败：cookie 过期")]
        out, added = RL.ensure_links(["记下了，但地图那边暂时写不进去"], ev, ["hz-food-map"], RULES)
        self.assertNotIn("link", added)

    def test_link_taken_only_from_recommend_results(self):
        # 别的工具返回里恰好也有这句，不算
        ev = [_call("1", "list_places"), _result("1", f"高德里打开：{LINK}")]
        out, added = RL.ensure_links(["列表如上"], ev, ["hz-food-map"], RULES)
        self.assertNotIn(LINK, out[0])

    def test_multiple_recorded_stores_get_named_links(self):
        ev = RECORDED + [_call("4", "recommend_place"),
                         _result("4", "已收录「灰太狼烤羊腿烧烤(狮山路店)」…\n高德里打开：https://food.bigsong.site/p/B0JK6BM6T3")]
        out, _ = RL.ensure_links(["两家都收了"], ev, ["hz-food-map"], RULES)
        self.assertIn("「遵义羊肉粉」高德里打开：" + LINK, out[0])
        self.assertIn("「灰太狼烤羊腿烧烤(狮山路店)」高德里打开：https://food.bigsong.site/p/B0JK6BM6T3", out[0])

    def test_already_recommended_return_also_yields_link(self):
        ev = [_call("1", "recommend_place"),
              _result("1", f"「遵义羊肉粉」已有人推荐过（小A），已把 松爸 加进推荐人。\n高德里打开：{LINK}")]
        out, added = RL.ensure_links(["这家小A推荐过，你也加上了"], ev, ["hz-food-map"], RULES)
        self.assertIn(LINK, out[0])

    def test_empty_bubbles_and_missing_rules(self):
        self.assertEqual(RL.ensure_links([], RECORDED, ["hz-food-map"], RULES), ([], []))
        self.assertEqual(RL.ensure_links(["x"], RECORDED, ["hz-food-map"], {}), (["x"], []))

    def test_malformed_events_do_not_crash(self):
        ev = [None, "x", {"type": "tool/result"}, {"type": "tool/call", "data": None},
              {"type": "tool/result", "data": {"message": {"content": "不是列表"}}}]
        out, added = RL.ensure_links(["收好了"], ev, ["hz-food-map"], RULES)
        self.assertEqual(out, ["收好了"])

    def test_does_not_mutate_input(self):
        src = ["收好了"]
        RL.ensure_links(src, RECORDED, ["hz-food-map"], RULES)
        self.assertEqual(src, ["收好了"])



# ---------------------------------------------------------------- 网关端到端：最终交给机器人的回复里真的补上了

import json, os, tempfile, time
from brain.gateway.config import DEFAULTS
from brain.gateway.dsh_client import TurnResult
from brain.gateway.server import Gateway


class _Dsh:
    """模拟一轮：模型先调 hzfood 工具（事件流里出现 tool/call + tool/result），再调 wx_reply 但漏了链接。"""
    def __init__(self, ref, bubbles, events):
        self.ref, self.bubbles, self.events = ref, bubbles, events
    def start(self): pass
    def initialize(self, cwd, provider, model): return {}
    def alive(self): return True
    def stop(self): pass
    def prompt(self, session_id, text, timeout_sec):
        self.ref[0].tool_call("wx_reply", {"bubbles": self.bubbles})
        return TurnResult(reasoning="", text="", events=self.events + [{"type": "turn/end"}])


class GatewayLinkTest(unittest.TestCase):
    def _run(self, conv, bubbles, events, text="你自己在高德搜一下这家，加进去"):
        data = tempfile.mkdtemp(); ws = os.path.join(data, "workspace")
        for d in ("knowledge", "memory/people", "skills"):
            os.makedirs(os.path.join(ws, d))
        open(os.path.join(ws, "knowledge", "shared.md"), "w").close()
        with open(os.path.join(ws, "skills", "index.json"), "w", encoding="utf-8") as f:
            json.dump({"hz-food-map": {"groups": ["共建杭州美食地图🐶"]}}, f)
        ref = []
        gw = Gateway(DEFAULTS, data, ws, dsh_factory=lambda: _Dsh(ref, bubbles, events))
        ref.append(gw)
        out = gw.handle_reply({"conversation": conv, "is_group": True, "sender": "松爸", "text": text})
        with open(os.path.join(data, "log", "replies-" + time.strftime("%Y%m%d") + ".jsonl"), encoding="utf-8") as f:
            log = f.read()
        return out, gw, json.loads(log.strip().splitlines()[-1])

    def test_food_group_record_gets_link_and_footer(self):
        """复现 09-11 20:03：收录成功、回复没带链接 —— 现在交给机器人的版本里补上了。"""
        out, gw, rec = self._run("共建杭州美食地图🐶", ["收好了：狮山路163号的遵义羊肉粉，进「吃」文件夹 🐾"], RECORDED)
        self.assertIn(f"高德里打开：{LINK}", out["bubbles"][-1])
        self.assertTrue(out["bubbles"][-1].endswith(FOOTER))
        self.assertEqual(rec["links_added"], ["link", "footer"])
        # recent 只记模型原话，固定结尾不进反重复比对
        self.assertNotIn("4bvd5QZ4HY", " ".join(gw.recent.recent_for("共建杭州美食地图🐶")))

    def test_other_group_untouched(self):
        out, _, rec = self._run("肥肉测试1🐶", ["收好了"], RECORDED)
        self.assertEqual(out["bubbles"], ["收好了"])
        self.assertEqual(rec["links_added"], [])

    def test_real_default_config_has_food_footer(self):
        self.assertIn("4bvd5QZ4HY", DEFAULTS["skill_links"]["hz-food-map"]["footer"])


class BatchLinkTest(unittest.TestCase):
    """recommend_batch 的返回是 JSON，每家 result 里都有「高德里打开：链接」。"""
    RULES = {"hz-food-map": {**RULES["hz-food-map"],
                             "link_tools": ["recommend_place", "recommend_from_link", "recommend_batch"],
                             "footer_tools": ["recommend_place", "recommend_from_link", "suggest", "recommend_batch"],
                             "max_links": 3}}

    def _batch_result(self, names):
        rec = [{"asked": n, "name": n, "result": f"已收录「{n}」，推荐人 沈凌军，已写入高德地图小程序（文件夹「吃」）。描述：\n@沈凌军：好吃\n"
                                              f"高德里打开：https://food.bigsong.site/p/B0{i:08d}"} for i, n in enumerate(names)]
        import json as _j
        return [_call("9", "recommend_batch"),
                _result("9", _j.dumps({"recorded": rec, "need_confirm": []}, ensure_ascii=False, indent=2))]

    def test_many_stores_get_footer_only(self):
        ev = self._batch_result([f"店{i}" for i in range(12)])
        out, added = RL.ensure_links(["收了 12 家：店0、店1……"], ev, ["hz-food-map"], self.RULES)
        self.assertEqual(added, ["footer"])
        self.assertNotIn("food.bigsong.site/p/", out[0])

    def test_single_store_batch_gets_its_link(self):
        ev = self._batch_result(["灰太狼烤羊腿烧烤(狮山路店)"])
        out, added = RL.ensure_links(["收好了"], ev, ["hz-food-map"], self.RULES)
        self.assertIn("高德里打开：https://food.bigsong.site/p/B000000000", out[0])
        self.assertNotIn('"', out[0])                    # JSON 的引号、逗号不能被当成链接的一部分吞进来

    def test_names_taken_per_store_not_folder(self):
        # 「文件夹「吃」」也是书名号，店名必须取每段开头那个，不能取到「吃」
        ev = self._batch_result(["店甲", "店乙"])
        out, _ = RL.ensure_links(["两家收了"], ev, ["hz-food-map"], self.RULES)
        self.assertIn("「店甲」高德里打开：https://food.bigsong.site/p/B000000000", out[0])
        self.assertIn("「店乙」高德里打开：https://food.bigsong.site/p/B000000001", out[0])
        self.assertNotIn("「吃」高德", out[0])

    def test_default_config_wires_batch(self):
        r = DEFAULTS["skill_links"]["hz-food-map"]
        self.assertIn("recommend_batch", r["link_tools"]); self.assertIn("recommend_batch", r["footer_tools"])

if __name__ == "__main__":
    unittest.main()
