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

    def test_声明没有联网能力(self):
        p = build_preamble(datetime(2026, 7, 30))
        self.assertIn('没有联网', p)
        self.assertIn('推特', p)

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
