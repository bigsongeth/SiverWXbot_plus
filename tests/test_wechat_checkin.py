import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from plugins.wechat_checkin.store import CheckinStore
from plugins.wechat_checkin.handler import is_checkin_trigger, build_user_key


def make_code(batch_date="2026-05-07", code="code-1", btc=20, expires_at=2000000000):
    return {
        "batch_date": batch_date,
        "batch_no": 1,
        "code": code,
        "quota": btc * 500000,
        "display_amount": str(btc),
        "display_unit": "🪙 BTC",
        "expires_at": expires_at,
    }


class Dummy:
    pass


class WechatCheckinTests(unittest.TestCase):
    def test_import_codes_and_claim_once_per_user_per_day(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "checkin.sqlite3"
            store = CheckinStore(db_path)
            store.import_codes([make_code(code="A", btc=20), make_code(code="B", btc=30)], source_file="seed.json")
            now = datetime(2026, 5, 7, 9, 0, tzinfo=timezone(timedelta(hours=8)))
            first = store.claim_code("user-a", "Alice", "签到", "msg-1", now=now)
            second = store.claim_code("user-a", "Alice", "签到", "msg-2", now=now)
            self.assertEqual(first.status, "success")
            self.assertEqual(first.code, "A")
            self.assertEqual(first.display_amount, "20")
            self.assertEqual(second.status, "already_claimed")
            self.assertEqual(second.code, "A")
            con = sqlite3.connect(db_path)
            self.assertEqual(con.execute("select count(*) from claim_records").fetchone()[0], 1)
            self.assertEqual(con.execute("select status from daily_codes where code='A'").fetchone()[0], "claimed")
            self.assertEqual(con.execute("select status from daily_codes where code='B'").fetchone()[0], "unused")
            con.close()

    def test_claim_skips_expired_codes_and_reports_no_code(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "checkin.sqlite3"
            store = CheckinStore(db_path)
            store.import_codes([make_code(code="old", expires_at=100)], source_file="seed.json")
            now = datetime(2026, 5, 7, 9, 0, tzinfo=timezone(timedelta(hours=8)))
            result = store.claim_code("user-a", "Alice", "签到", "msg-1", now=now)
            self.assertEqual(result.status, "no_code")
            self.assertIsNone(result.code)

    def test_trigger_matching_is_private_and_exact_enough(self):
        self.assertTrue(is_checkin_trigger("签到"))
        self.assertTrue(is_checkin_trigger(" 签到！"))
        self.assertTrue(is_checkin_trigger("领码"))
        self.assertFalse(is_checkin_trigger("我想问一下签到规则"))
        self.assertFalse(is_checkin_trigger("hello"))

    def test_build_user_key_prefers_stable_available_fields(self):
        chat = Dummy()
        message = Dummy()
        chat.who = "Alice"
        message.sender = "Alice"
        self.assertEqual(build_user_key(chat, message), "Alice")
        chat.wxid = "wxid_123"
        self.assertEqual(build_user_key(chat, message), "wxid_123")


if __name__ == "__main__":
    unittest.main()
