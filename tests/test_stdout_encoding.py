"""stdout/stderr 编码兜底回归测试。

背景：wxautox4 41.x 会打印登录昵称（本机是「🐶肥肉」）。Windows 下 stdout 被
重定向到文件时默认 GBK，编不了 emoji 直接抛 UnicodeEncodeError，被
init_wx_listeners 的 except 接住，只留一句「初始化微信监听器失败，请检查微信
是否启动登录正确」——完全指错方向。2026-08-11、2026-09-02 各栽一次。

这里用子进程把 stdout 强制成 gbk，验证 import logger 之后打印 emoji 不会崩。
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# wxautox4 41.x 真实打印的那句，position 15 正好是 🐶
WX_LOG_LINE = "初始化成功，获取到已登录窗口：\U0001f436肥肉（使用缓存）"


def _run_under_gbk(code):
    env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONPATH=ROOT)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=env, capture_output=True,
    )


class TestStdoutEncodingGuard(unittest.TestCase):
    def test_gbk_really_cannot_encode_the_emoji(self):
        """先证明没有兜底时确实会崩，免得这条测试变成永远绿的摆设。"""
        p = _run_under_gbk('print(%r)' % WX_LOG_LINE)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("UnicodeEncodeError", p.stderr.decode("utf-8", "replace"))

    def test_import_logger_makes_stdout_emoji_safe(self):
        p = _run_under_gbk('import logger; print(%r)' % WX_LOG_LINE)
        self.assertEqual(
            p.returncode, 0,
            "import logger 后打印 emoji 仍然崩了：\n"
            + p.stderr.decode("utf-8", "replace"),
        )

    def test_import_logger_switches_both_streams_to_utf8(self):
        """stderr 默认是 backslashreplace 本来就不崩，所以直接查编码本身。"""
        p = _run_under_gbk(
            'import logger, sys;'
            ' print(sys.stdout.encoding, sys.stderr.encoding)')
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = p.stdout.decode("utf-8", "replace").split()
        self.assertEqual([s.lower() for s in out], ["utf-8", "utf-8"], out)


if __name__ == "__main__":
    unittest.main()
