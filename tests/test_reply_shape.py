# -*- coding: utf-8 -*-
"""reply_shape 插件单测：分条形状整理（纯函数，不碰微信、不发请求）。

mac 上直接跑文件：PYTHONPATH=. python3 tests/test_reply_shape.py
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest

from plugins import reply_shape
from plugins.reply_shape import store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="replyshape_")
        self._d, self._c = store.DATA_DIR, store.CONFIG_PATH
        store.DATA_DIR = self.tmp
        store.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        store._cache = None
        store._cache_mtime = None

    def tearDown(self):
        store.DATA_DIR, store.CONFIG_PATH = self._d, self._c
        store._cache = None
        store._cache_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMergeThinParts(Base):
    # 2026-08-03 群里的真实回复：6 条顶满上限，只有中间 3 条有内容。
    截图原样 = [
        "肥肉在键盘上打了个盹，醒来就刷到了 DeepSeek-V4-Flash-0731这波新模型。",
        "X上大家推得飞起：284B参数 MoE，只激活13B，成本低到离谱，跑本地 Mac 也能当日常工具用。",
        "但也有小吐槽——推理清晰度不如大模型，指令遵循有时要自己补救。",
        "总的来说，性价比炸裂，适合 Agent 和本地部署。",
        "想看最新推文？肥肉给您扫了。",
        "需要我继续挖？发个信号就行。",
    ]

    def test_收尾的两条废话被并进上一条(self):
        out = reply_shape.merge_thin_parts(self.截图原样, 150)
        self.assertLess(len(out), len(self.截图原样))
        # "想看最新推文"和"需要我继续挖"不再各自占一个气泡
        self.assertFalse(any(p.startswith("想看最新推文") for p in out))
        self.assertFalse(any(p.startswith("需要我继续挖") for p in out))
        # 但内容一个字都没丢
        self.assertIn("想看最新推文", "".join(out))
        self.assertIn("需要我继续挖", "".join(out))

    def test_有内容的条目原样保留(self):
        out = reply_shape.merge_thin_parts(self.截图原样, 150)
        self.assertIn("但也有小吐槽——推理清晰度不如大模型，指令遵循有时要自己补救。", out)

    def test_合并后不撑破单条字数上限(self):
        out = reply_shape.merge_thin_parts(self.截图原样, 150)
        for p in out:
            self.assertLessEqual(len(p), 150, p)

    def test_合并会撑破上限时就不合(self):
        long_one = "长" * 148
        out = reply_shape.merge_thin_parts([long_one, "短句"], 150)
        self.assertEqual(out, [long_one, "短句"])   # 148+2+1 > 150，保持原样

    def test_首条过短时向后合并(self):
        """首条太短的情况，循环里碰不到（那时还没有上一条），要单独往后并一次。"""
        out = reply_shape.merge_thin_parts(["我看看啊", "DeepSeek 出了 V4-Flash，284B 参数只激活 13B。"], 150)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("我看看啊"))

    def test_单条与空输入不动(self):
        self.assertEqual(reply_shape.merge_thin_parts(["就一条"], 150), ["就一条"])
        self.assertEqual(reply_shape.merge_thin_parts([], 150), [])

    def test_关掉开关就完全不介入(self):
        cfg = reply_shape.get_config()
        cfg["enabled"] = False
        reply_shape.save_config(cfg)
        self.assertEqual(reply_shape.merge_thin_parts(self.截图原样, 150), self.截图原样)

    def test_脏的字数上限不炸(self):
        out = reply_shape.merge_thin_parts(["短", "也短"], None)
        self.assertEqual(len(out), 1)


class TestStripMarkdown(Base):
    # 2026-08-03 融合版人设实测的真实输出：人设里明写了"不要用 Markdown"，
    # 散文式回答很干净，一到分类罗列就破戒。prompt 管不住，得在发送前兜一道。
    据点回答 = (
        "汪！肥肉我趴在这儿呢，正好给你说说。\n\n"
        "NCC 的据点分三类哈：\n\n"
        "**一、自营共居据点（可以长住）：**\n"
        "- 云南大理 —— NCC 的起点，宠物友好。\n"
        "- 黄山黟县（黑多岛）—— 安徽那边。\n\n"
        "---\n\n"
        "想去的话加小助手：nccxiaozhushou 🐶"
    )

    def test_剥掉加粗星号(self):
        out = reply_shape.strip_markdown(self.据点回答)
        self.assertNotIn("**", out)
        self.assertIn("一、自营共居据点（可以长住）：", out)   # 文字本身一个不少

    def test_剥掉分隔线(self):
        out = reply_shape.strip_markdown(self.据点回答)
        self.assertNotIn("---", out)

    def test_保留列表符号(self):
        """行首的 - 当纯文本读也清楚，剥了反而分不清层次。"""
        out = reply_shape.strip_markdown(self.据点回答)
        self.assertIn("- 云南大理", out)

    def test_剥掉标题井号(self):
        out = reply_shape.strip_markdown("# 大标题\n### 小标题\n正文")
        self.assertEqual(out, "大标题\n小标题\n正文")

    def test_代码块内部原样不动(self):
        """肥肉会讲技术。围栏剥了代码会糊成一坨，块内符号也一律不碰。"""
        src = "看这个：\n```python\ndef __init__(self):\n    x = a ** 2\n```\n就这样"
        out = reply_shape.strip_markdown(src)
        self.assertIn("```python", out)
        self.assertIn("a ** 2", out)          # 代码里的 ** 是乘方，不是加粗
        self.assertIn("__init__", out)

    def test_行内代码不动(self):
        out = reply_shape.strip_markdown("用 `a ** b` 算乘方")
        self.assertIn("`a ** b`", out)

    def test_列表项不会被当成分隔线(self):
        out = reply_shape.strip_markdown("- 大理\n- 黄山")
        self.assertEqual(out, "- 大理\n- 黄山")

    def test_关掉开关就原样返回(self):
        cfg = reply_shape.get_config()
        cfg["enabled"] = False
        reply_shape.save_config(cfg)
        self.assertEqual(reply_shape.strip_markdown(self.据点回答), self.据点回答)

    def test_空输入不炸(self):
        self.assertEqual(reply_shape.strip_markdown(""), "")
        self.assertIsNone(reply_shape.strip_markdown(None))


class TestAugmentSplitPrompt(Base):
    上游模板 = ("【回复格式要求】\n你可以自行决定是否将回复拆分为多条消息。\n"
                "【以下是你的角色设定】\n你是肥肉。")

    def test_约束插在角色设定之前(self):
        out = reply_shape.augment_split_prompt(self.上游模板)
        self.assertIn("每条都要有信息量", out)
        self.assertLess(out.index("每条都要有信息量"), out.index("【以下是你的角色设定】"))
        self.assertTrue(out.endswith("你是肥肉。"))    # 人设原样保留在最后

    def test_没有标记时追加到末尾(self):
        out = reply_shape.augment_split_prompt("随便一段提示词")
        self.assertIn("每条都要有信息量", out)

    def test_关掉开关就原样返回(self):
        cfg = reply_shape.get_config()
        cfg["enabled"] = False
        reply_shape.save_config(cfg)
        self.assertEqual(reply_shape.augment_split_prompt(self.上游模板), self.上游模板)

    def test_空输入不炸(self):
        self.assertEqual(reply_shape.augment_split_prompt(""), "")


if __name__ == '__main__':
    unittest.main(verbosity=2)
