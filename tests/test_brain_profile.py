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

    def test_prepare_dsh_home_writes_settings_once(self):
        d = tempfile.mkdtemp()
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        s = open(os.path.join(d, "settings.yaml"), encoding="utf-8").read()
        self.assertIn("model: songkey-auto", s)
        with open(os.path.join(d, "settings.yaml"), "a", encoding="utf-8") as f:
            f.write("# 人手改过\n")
        profile.prepare_dsh_home(d, "sk-x", "songkey-auto")
        self.assertIn("人手改过", open(os.path.join(d, "settings.yaml"), encoding="utf-8").read())

    def test_dsh_argv(self):
        self.assertEqual(profile.dsh_argv("node", "/d/bin.js", "/p.yml"),
                         ["node", "/d/bin.js", "--profile", "sdk", "--patch", "/p.yml"])


if __name__ == "__main__":
    unittest.main()
