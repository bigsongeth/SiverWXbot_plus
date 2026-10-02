# -*- coding: utf-8 -*-
"""
文字消息的 AI 调用必须经过 _get_group_api / _get_chat_api —— 大脑和备用链的钩子都挂在那儿。

背景：上游 v4.7.33 加「备用接口」时把群聊/私聊调用改成
    self._call_api_with_fallback(group_api_index, ...)
只传下标、按下标重建接口实例，整条绕开 _get_group_api。git 自动合并不报冲突，
合进来就是：生产上所有会话悄悄不走肥肉大脑（dsh_brain）、也不走 model_fallback 备用链，
退回直连老接口。合并时拦下，改成把 api=self._get_group_api(chat.who) 传进去。
这个测试守住这一点，下次合并上游再被冲掉会直接失败。

不 import wxbot_core（会连带拉起 wxautox），用 ast 摘方法出来 exec。
跑法：PYTHONPATH=. python3 tests/test_api_routing.py
"""
import ast
import os
import unittest

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wxbot_core.py')
API_ERROR_REPLY = "API返回错误，请稍后再试"

with open(CORE, encoding='utf-8') as _f:
    TREE = ast.parse(_f.read())


def _fallback_calls():
    return [n for n in ast.walk(TREE)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == '_call_api_with_fallback']


def _load_method(cls_name, meth_name):
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == meth_name)
    ns = {'API_ERROR_REPLY': API_ERROR_REPLY,
          'log': lambda **kw: None,
          '_shorten_log_text': str}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), CORE, 'exec'), ns)
    return ns[meth_name]


class TestCallSitesCarryHookedApi(unittest.TestCase):
    """凡是按会话选接口的文字调用，都得把钩子版的实例传进去。"""

    WANT = {'group_api_index': '_get_group_api', 'chat_api_index': '_get_chat_api'}

    def test_text_call_sites_pass_hooked_api(self):
        checked = 0
        for call in _fallback_calls():
            first = call.args[0] if call.args else None
            if not (isinstance(first, ast.Name) and first.id in self.WANT):
                continue  # 图片识别那几处按「图片识别接口」下标走，本来就不挂大脑
            checked += 1
            kw = {k.arg: k.value for k in call.keywords}
            self.assertIn('api', kw, f'第 {call.lineno} 行 _call_api_with_fallback 没传 api=，会绕开大脑/备用链钩子')
            v = kw['api']
            self.assertTrue(isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                            and v.func.attr == self.WANT[first.id],
                            f'第 {call.lineno} 行 api= 应该是 self.{self.WANT[first.id]}(...)')
        self.assertEqual(checked, 4, '群聊 2 处 + 私聊 2 处文字调用，数量变了请核对')

    def test_hooks_still_inside_get_api(self):
        src = ast.get_source_segment(open(CORE, encoding='utf-8').read(),
                                     next(n for n in ast.walk(TREE)
                                          if isinstance(n, ast.FunctionDef) and n.name == '_get_group_api'))
        self.assertIn('_with_fallback', src)
        self.assertIn('_resolve_group_api', src)


class FakeApi:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def chat(self, message, **kw):
        self.calls += 1
        return self.reply


class FakeBot:
    def __init__(self, configs, built):
        self.config = type('C', (), {'api_configs': configs, 'api_index': 0})()
        self.built = built  # 按下标重建出来的实例

    def _normalize_api_index(self, idx):
        return idx

    def _get_api_instance_by_index(self, idx):
        return self.built[idx]


CALL = _load_method('WXBot', '_call_api_with_fallback')


class TestCallApiWithFallback(unittest.TestCase):
    def test_uses_passed_api_not_index_rebuild(self):
        brain = FakeApi('大脑的回复')
        by_index = FakeApi('老接口的回复')
        bot = FakeBot([{'fallback_api_index': -1}], {0: by_index})
        self.assertEqual(CALL(bot, 0, 'hi', api=brain, prompt='p', history=[]), '大脑的回复')
        self.assertEqual(by_index.calls, 0)

    def test_upstream_fallback_still_works_after_hooked_api_fails(self):
        brain = FakeApi(API_ERROR_REPLY)
        backup = FakeApi('备用接口回复')
        bot = FakeBot([{'fallback_api_index': 1}, {'fallback_api_index': -1}], {1: backup})
        self.assertEqual(CALL(bot, 0, 'hi', api=brain), '备用接口回复')
        self.assertEqual((brain.calls, backup.calls), (1, 1))

    def test_no_reply_marker_is_a_success(self):
        brain = FakeApi('[NO_REPLY]')
        backup = FakeApi('不该被调到')
        bot = FakeBot([{'fallback_api_index': 1}, {}], {1: backup})
        self.assertEqual(CALL(bot, 0, 'hi', api=brain), '[NO_REPLY]')
        self.assertEqual(backup.calls, 0)


if __name__ == '__main__':
    unittest.main()
