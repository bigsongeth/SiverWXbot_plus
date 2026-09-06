# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import tempfile
import unittest
from brain.gateway import profile


class ProfileTest(unittest.TestCase):
    def test_render_patch_indents_persona_and_args(self):
        txt = profile.render_patch("你是肥肉。\n第二行。", ["/usr/bin/python3", "/x/feirou_tools.py"], None, "sk-test")
        self.assertIn("    persona: |\n      你是肥肉。\n      第二行。\n", txt)
        self.assertIn("        command: /usr/bin/python3\n        args:\n          - /x/feirou_tools.py\n", txt)
        self.assertNotIn("__GROK_BLOCK__", txt)
        self.assertNotIn("sk-test", txt)  # 没给 grok 就不该出现 key

    def test_render_patch_with_grok(self):
        txt = profile.render_patch("p", ["py", "t.py"], "/srv/server.js", "sk-test")
        self.assertIn("serverName: grok", txt)
        self.assertIn("SONGKEY_API_KEY: sk-test", txt)

    def test_bridge_gateway_url_follows_bind(self):
        self.assertEqual(profile.bridge_gateway_url("127.0.0.1", 8500), "http://127.0.0.1:8500")
        self.assertEqual(profile.bridge_gateway_url("0.0.0.0", 8500), "http://127.0.0.1:8500")
        self.assertEqual(profile.bridge_gateway_url("100.127.39.63", 8500), "http://100.127.39.63:8500")

    def test_prepare_dsh_home_links_workspace_skills(self):
        d = tempfile.mkdtemp()
        skills = os.path.join(d, "ws-skills"); os.makedirs(skills)
        home = os.path.join(d, "dsh-home")
        profile.prepare_dsh_home(home, "sk-x", "songkey-auto", skills_dir=skills)
        self.assertEqual(os.readlink(os.path.join(home, "skills")), skills)
        other = os.path.join(d, "other"); os.makedirs(other)
        profile.prepare_dsh_home(home, "sk-x", "songkey-auto", skills_dir=other)   # 换目录要重指
        self.assertEqual(os.readlink(os.path.join(home, "skills")), other)

    def test_prepare_dsh_home_writes_settings_once(self):
        d = tempfile.mkdtemp()
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        with open(os.path.join(d, "settings.yaml"), encoding="utf-8") as f:
            s = f.read()
        self.assertIn("model: songkey-auto", s)
        with open(os.path.join(d, "settings.yaml"), "a", encoding="utf-8") as f:
            f.write("# 人手改过\n")
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        with open(os.path.join(d, "settings.yaml"), encoding="utf-8") as f:
            s2 = f.read()
        self.assertIn("人手改过", s2)

    def test_dsh_argv(self):
        self.assertEqual(profile.dsh_argv("node", "/d/bin.js", "/p.yml"),
                         ["node", "/d/bin.js", "--profile", "sdk", "--patch", "/p.yml"])

    def _find_by_id(self, doc, target_id):
        """在渲染出的 patch 结构里递归找 id == target_id 的条目（有的条目嵌在
        `insert:` 列表下面，不是顶层元素）。"""
        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("id") == target_id:
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(doc)
        return found

    def test_render_patch_is_valid_yaml_with_multiline_persona(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML 未安装，跳过 YAML 解析校验")
            return
        persona = "你是肥肉。\n第二行。"
        argv = ["/usr/bin/python3", "/x/feirou_tools.py", "--extra-arg"]
        txt = profile.render_patch(persona, argv, "/srv/server.js", "sk-test")
        doc = yaml.safe_load(txt)
        self.assertIsInstance(doc, list)

        mcp_feirou = self._find_by_id(doc, "mcp-feirou")
        self.assertEqual(len(mcp_feirou), 1)
        self.assertEqual(mcp_feirou[0]["config"]["args"], argv[1:])

        sysprompt = self._find_by_id(doc, "system-prompt")
        self.assertEqual(len(sysprompt), 1)
        self.assertEqual(sysprompt[0]["config"]["persona"], persona + "\n")

        mcp_grok = self._find_by_id(doc, "mcp-grok")
        self.assertEqual(len(mcp_grok), 1)
        self.assertEqual(mcp_grok[0]["config"]["env"]["SONGKEY_API_KEY"], "sk-test")

    def test_render_patch_is_valid_yaml_without_grok_single_arg(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML 未安装，跳过 YAML 解析校验")
            return
        persona = "单行人设"
        argv = ["py", "t.py"]
        txt = profile.render_patch(persona, argv, None, "sk-test")
        doc = yaml.safe_load(txt)
        self.assertIsInstance(doc, list)

        mcp_feirou = self._find_by_id(doc, "mcp-feirou")
        self.assertEqual(len(mcp_feirou), 1)
        self.assertEqual(mcp_feirou[0]["config"]["args"], argv[1:])

        sysprompt = self._find_by_id(doc, "system-prompt")
        self.assertEqual(sysprompt[0]["config"]["persona"], persona + "\n")

        self.assertEqual(self._find_by_id(doc, "mcp-grok"), [])


if __name__ == "__main__":
    unittest.main()
