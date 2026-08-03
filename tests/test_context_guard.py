# -*- coding: utf-8 -*-
"""context_guard 单测（纯函数，不碰微信、不发请求）。
mac 上跑：PYTHONPATH=. python3 tests/test_context_guard.py
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.context_guard import augment_prompt, build_preamble, filter_history


class TestPreamble(unittest.TestCase):
    def test_日期按注入时间渲染(self):
        p = build_preamble(datetime(2026, 7, 30))
        self.assertIn('2026年07月30日', p)
        self.assertIn('星期四', p)

    def test_要求真搜到才说而不是断言没有联网(self):
        """2026-08-03 改：原来写死"你没有联网"，把 grok 这类真有搜索能力的模型也摁死了
        （松爸问推特被答"我没法联网"，其实 grok 搜得到）。现在改成能力无关的说法，
        对有搜索和没搜索的模型都成立，机器人侧不必猜最终用的是哪个模型。"""
        p = build_preamble(datetime(2026, 7, 30))
        self.assertNotIn('你没有联网', p)      # 不再断言模型没有这个能力
        self.assertIn('先用搜索工具查一遍', p)  # 有工具就得真搜（祈使句，别写成"如果配了工具"的条件句，
                                              # 条件句会让 grok 自认为没工具，绕过搜索直接编）
        self.assertIn('查不到', p)             # 没工具/没搜到就得承认
        self.assertIn('推特', p)               # "没真搜就别说刚刷了推特"这条要留着

    def test_禁止复读上下文文案(self):
        self.assertIn('不要复读', build_preamble(datetime(2026, 7, 30)))

    def test_追加在人设之后而非替换(self):
        out = augment_prompt('你是肥肉，一只法斗。', now=datetime(2026, 7, 30))
        self.assertTrue(out.startswith('你是肥肉，一只法斗。'))
        self.assertIn('2026年07月30日', out)

    def test_空人设不凭空造(self):
        self.assertEqual(augment_prompt(''), '')
        self.assertIsNone(augment_prompt(None))


class TestFilterHistory(unittest.TestCase):
    # 2026-08-03：松爸连问三次"搜一下推特"，肥肉每次都答"我这边没法联网"。
    # 路由是对的（确实走了有搜索能力的 grok），病根是历史里那几条自我否定被模型
    # 当成行为范例照着复读——去掉它们再问，同一个模型立刻真搜并给出 x.com 引用。
    _松爸历史 = [
        {"time": "2026/08/03 02:45:39", "type": "text", "attr": "friend",
         "sender": "松爸", "content": "你能搜索一下推特上 DeepSeek 的评价吗？"},
        {"time": "2026/08/03 02:46:06", "type": "text", "attr": "self",
         "sender": "self", "content": "我这边没法联网，也看不到推特/X，别让我硬编 😂"},
        {"time": "2026/08/03 02:46:06", "type": "text", "attr": "self",
         "sender": "self", "content": "你把链接、截图或几条推文贴过来，我可以帮你提炼。"},
        {"time": "2026/08/03 02:50:00", "type": "text", "attr": "friend",
         "sender": "松爸", "content": "NCC 谁是主理人？"},
        {"time": "2026/08/03 02:50:10", "type": "text", "attr": "self",
         "sender": "self", "content": "主理人是大曹，微信 ShariCao。"},
    ]

    def test_丢掉机器人自称没联网的历史(self):
        out = filter_history(self._松爸历史)
        self.assertNotIn('没法联网', ''.join(m['content'] for m in out))

    def test_分段回复的另一半按同一时刻连坐(self):
        """||SPLIT|| 拆出来的多条共享同一个 time，关键词只落在其中一条上。
        只丢那条的话，"你把链接贴过来"照样留在历史里当行为范例。"""
        out = filter_history(self._松爸历史)
        self.assertNotIn('贴过来', ''.join(m['content'] for m in out))

    def test_坏回复对应的用户提问一起丢(self):
        """只删机器人那半边会留下孤零零的提问，模型看到"问了没人应"会判定不用接话
        （实测直接回 [NO_REPLY]，静默不回复比答错更糟）。"""
        out = filter_history(self._松爸历史)
        self.assertNotIn('搜索一下推特', ''.join(m['content'] for m in out))

    def test_连坐不误伤无关轮次(self):
        """同一份历史里正常的那轮（问主理人）必须原样保留。"""
        out = filter_history(self._松爸历史)
        joined = ''.join(m['content'] for m in out)
        self.assertIn('NCC 谁是主理人', joined)
        self.assertIn('ShariCao', joined)
        self.assertEqual(len(out), 2)

    def test_用户说的没法联网不被丢(self):
        """子串规则只管机器人自己的发言——用户问"你是不是没法联网"得留着。"""
        msgs = [{"time": "2026/08/03 02:45:39", "type": "text", "attr": "friend",
                 "sender": "松爸", "content": "你是不是没法联网啊？"}]
        self.assertEqual(len(filter_history(msgs)), 1)

    def test_丢掉系统时间戳条目(self):
        msgs = [
            {"time": "2026/07/30 02:06:27", "type": "time", "attr": "system",
             "sender": "system", "content": "02:06"},
            {"time": "2026/07/30 02:07:00", "type": "text", "attr": "friend",
             "sender": "松爸", "content": "在吗"},
        ]
        out = filter_history(msgs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'], '在吗')

    def test_丢掉API兜底文案(self):
        msgs = [
            {"type": "text", "attr": "self", "sender": "self", "content": "在忙，我稍后回复您"},
            {"type": "text", "attr": "self", "sender": "self", "content": "API返回错误，请稍后再试"},
            {"type": "text", "attr": "self", "sender": "self", "content": "汪！"},
        ]
        self.assertEqual([m['content'] for m in filter_history(msgs)], ['汪！'])

    def test_丢掉NO_REPLY标记(self):
        msgs = [{"type": "text", "attr": "self", "sender": "self", "content": "[NO_REPLY]"}]
        self.assertEqual(filter_history(msgs), [])

    def test_丢掉空内容(self):
        msgs = [{"type": "text", "attr": "friend", "sender": "松爸", "content": "   "}]
        self.assertEqual(filter_history(msgs), [])

    def test_正常对话原样保留且顺序不变(self):
        msgs = [
            {"type": "text", "attr": "friend", "sender": "松爸", "content": "签到"},
            {"type": "text", "attr": "self", "sender": "self", "content": "签到成功 ✅"},
            {"type": "text", "attr": "friend", "sender": "松爸", "content": "谢谢"},
        ]
        self.assertEqual(filter_history(msgs), msgs)

    def test_extra_drop可追加(self):
        msgs = [{"type": "text", "attr": "self", "sender": "self", "content": "自定义兜底"}]
        self.assertEqual(filter_history(msgs, extra_drop=['自定义兜底']), [])

    def test_空输入与脏数据不炸(self):
        self.assertEqual(filter_history([]), [])
        self.assertEqual(filter_history(None), None)
        self.assertEqual(filter_history(['不是字典', None]), [])

    def test_真实松爸记忆清洗效果(self):
        """用线上那份被污染的记忆做回归：噪音条目确实被清掉。"""
        import json
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'memory', 'FeiRou_NCC', '松爸', '松爸_memory.json')
        if not os.path.exists(path):
            self.skipTest('线上记忆文件不在，跳过')
        with open(path, encoding='utf-8') as f:
            msgs = json.load(f)
        out = filter_history(msgs)
        self.assertLess(len(out), len(msgs))
        for m in out:
            self.assertNotEqual(m.get('attr'), 'system')
            self.assertNotIn(m.get('content'), ('在忙，我稍后回复您', '[NO_REPLY]'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
