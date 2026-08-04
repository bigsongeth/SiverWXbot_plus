# -*- coding: utf-8 -*-
"""
MainWindowChat 回落通道的鸭子类型契约测试。

背景：AddListenChat 弹不出子窗口时，全局模式会用 MainWindowChat 回落到主窗口回复。
这个假 chat 对象必须凑齐 process_message 会读到的属性，少一个就 AttributeError，
异常被 main 的兜底 except 接住，而消息已被 GetNextNewMessage 消费掉 —— 无声丢失。
2026-08-03 私聊「签到」就是这么丢的（缺 chat_type）。

不 import wxbot_core（会连带拉起 wxautox，mac 上跑不了），直接用 ast 把类摘出来 exec。
跑法：PYTHONPATH=. python3 tests/test_main_window_chat.py
"""
import ast
import os
import sys
import unittest

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wxbot_core.py')


def _load_class(name):
    """从 wxbot_core.py 里单独摘一个 class 出来 exec，不触发模块级 import。"""
    with open(CORE, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), CORE, 'exec'), ns)
    return ns[name]


MainWindowChat = _load_class('MainWindowChat')


class FakeWX:
    def __init__(self):
        self.calls = []

    def SendMsg(self, msg, who, exact):
        self.calls.append((msg, who, exact))
        return True


class TestMainWindowChat(unittest.TestCase):

    def test_has_who(self):
        c = MainWindowChat(FakeWX(), 'King_🐕')
        self.assertEqual(c.who, 'King_🐕')

    def test_has_chat_type_default_friend(self):
        """process_message / message_handle_callback 直接读 chat.chat_type，必须存在。"""
        c = MainWindowChat(FakeWX(), 'King_🐕')
        self.assertEqual(c.chat_type, 'friend')

    def test_chat_type_overridable(self):
        c = MainWindowChat(FakeWX(), '某群', chat_type='group')
        self.assertEqual(c.chat_type, 'group')

    def test_private_chat_not_skipped_by_global_mode_gate(self):
        """复刻 wxbot_core.py 约 3443 行：全局模式下 chat_type=='group' 才跳过 AI 回复。"""
        c = MainWindowChat(FakeWX(), 'King_🐕')
        self.assertFalse(c.chat_type == 'group')

    def test_image_gate_treats_fallback_as_private(self):
        """复刻约 3159 行：全局模式下非群聊才按私聊图片开关处理。"""
        c = MainWindowChat(FakeWX(), 'King_🐕')
        self.assertTrue(c.chat_type != 'group')

    def test_send_msg_goes_through_main_window(self):
        wx = FakeWX()
        c = MainWindowChat(wx, 'King_🐕')
        self.assertTrue(c.SendMsg('兑换码：xxx'))
        self.assertEqual(wx.calls, [('兑换码：xxx', 'King_🐕', True)])

    def test_send_msg_tolerates_extra_kwargs(self):
        """上层可能带 at_sender/quote 等 kwarg，回落通道不该因此炸掉。"""
        wx = FakeWX()
        c = MainWindowChat(wx, 'King_🐕')
        self.assertTrue(c.SendMsg('hi', at_sender=True, quote=False))
        self.assertEqual(wx.calls, [('hi', 'King_🐕', True)])

    def test_contract_covers_all_attrs_read_by_core(self):
        """
        兜底：扫一遍 wxbot_core.py 里 `chat.xxx` 的读取，确认回落通道没漏属性。
        只扫 process_message / wx_send_ai / message_handle_callback 三个函数体。
        """
        with open(CORE, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        wanted = {'process_message', 'wx_send_ai', 'message_handle_callback'}
        attrs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                            and sub.value.id == 'chat'):
                        attrs.add(sub.attr)
        c = MainWindowChat(FakeWX(), 'King_🐕')
        missing = sorted(a for a in attrs if not hasattr(c, a))
        self.assertEqual(missing, [], f'MainWindowChat 缺少核心会用到的属性：{missing}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
