# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import tempfile
import unittest
from brain.gateway.replies import RecentReplies


class RecentRepliesTest(unittest.TestCase):
    def test_add_and_recent_merges_conv_and_global(self):
        p = os.path.join(tempfile.mkdtemp(), "recent.json")
        r = RecentReplies(p, global_n=3, per_conv_n=2)
        r.add("A", ["a1", "a2", "a3"])
        r.add("B", ["b1"])
        self.assertEqual(r.recent_for("A"), ["a2", "a3", "b1"])
        self.assertEqual(r.recent_for("C"), ["a2", "a3", "b1"])

    def test_persists(self):
        p = os.path.join(tempfile.mkdtemp(), "recent.json")
        RecentReplies(p, 5, 5).add("A", ["x"])
        self.assertEqual(RecentReplies(p, 5, 5).recent_for("A"), ["x"])


if __name__ == "__main__":
    unittest.main()
