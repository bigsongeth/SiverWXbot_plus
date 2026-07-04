import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from plugins.wechat_checkin.store import CheckinStore, ClaimResult
from plugins.wechat_checkin.handler import is_checkin_trigger, build_user_key, format_reply


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

    def test_claim_uses_utc_day_boundary_before_beijing_8am(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "checkin.sqlite3"
            store = CheckinStore(db_path)
            # 兑换码批次按 UTC 日期切日：UTC 2026-05-11 = 北京时间 05-11 08:00 到 05-12 08:00
            expires_at = int(datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp())
            store.import_codes([make_code(batch_date="2026-05-11", code="utc-day-code", expires_at=expires_at)], source_file="seed.json")
            now = datetime(2026, 5, 12, 7, 30, tzinfo=timezone(timedelta(hours=8)))

            result = store.claim_code("user-a", "Alice", "签到", "msg-1", now=now)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.code, "utc-day-code")

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

    def test_success_reply_copywriting(self):
        result = ClaimResult(
            status="success",
            code="b31037e3b82752fb7f22855b6fdd6ad3",
            quota=80000000,
            display_amount="160",
            display_unit="🪙 BTC",
            expired_at=1778198400,
        )
        self.assertEqual(
            format_reply(result),
            "签到成功 ✅\n今天给你抽到：160 🪙 BTC\n兑换码：b31037e3b82752fb7f22855b6fdd6ad3\n早八前有效哦。\n登录松 Key，在钱包管理页面兑换。",
        )


    def test_already_claimed_reply_is_short(self):
        result = ClaimResult(
            status="already_claimed",
            code="b31037e3b82752fb7f22855b6fdd6ad3",
            quota=80000000,
            display_amount="160",
            display_unit="🪙 BTC",
            expired_at=1778198400,
        )
        self.assertEqual(format_reply(result), "你今天已经领过啦 ✅")
        self.assertNotIn("兑换码", format_reply(result))
        self.assertNotIn("今天给你抽到", format_reply(result))


if __name__ == "__main__":
    unittest.main()
