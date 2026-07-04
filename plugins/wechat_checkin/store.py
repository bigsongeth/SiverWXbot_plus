from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, Optional

TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

@dataclass
class ClaimResult:
    status: str
    code: Optional[str] = None
    quota: Optional[int] = None
    display_amount: Optional[str] = None
    display_unit: str = "🪙 BTC"
    expired_at: Optional[int] = None
    claimed_at: Optional[str] = None

class CheckinStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self):
        con = sqlite3.connect(str(self.db_path), timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def init_db(self):
        con = self.connect()
        try:
            con.executescript("""
                create table if not exists daily_codes (
                    id integer primary key autoincrement,
                    batch_date text not null,
                    batch_no integer default 1,
                    code text not null unique,
                    quota integer not null,
                    display_amount text not null,
                    display_unit text not null default '🪙 BTC',
                    expired_at integer not null,
                    status text not null default 'unused',
                    claimed_by text,
                    claimed_name text,
                    claimed_at text,
                    source_file text,
                    imported_at text not null default (datetime('now'))
                );
                create index if not exists idx_daily_codes_pick on daily_codes(batch_date, status, expired_at, id);
                create table if not exists claim_records (
                    id integer primary key autoincrement,
                    user_key text not null,
                    user_name text,
                    claim_date text not null,
                    code text not null unique,
                    quota integer not null,
                    display_amount text not null,
                    display_unit text not null,
                    message_text text,
                    message_id text,
                    created_at text not null,
                    status text not null default 'success',
                    unique(user_key, claim_date)
                );
            """)
            con.commit()
        finally:
            con.close()

    def import_codes(self, codes: Iterable[dict], source_file: str = "") -> int:
        now = datetime.now(TZ).isoformat(timespec="seconds")
        rows = []
        for item in codes:
            rows.append((str(item["batch_date"]), int(item.get("batch_no", 1)), str(item["code"]),
                         int(item["quota"]), str(item["display_amount"]),
                         str(item.get("display_unit", "🪙 BTC")).strip() or "🪙 BTC",
                         int(item["expires_at"]), source_file, now))
        con = self.connect()
        try:
            cur = con.executemany("""
                insert or ignore into daily_codes
                (batch_date, batch_no, code, quota, display_amount, display_unit, expired_at, source_file, imported_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            con.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        finally:
            con.close()

    def import_json_file(self, path: str | Path) -> int:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        codes = data.get("codes", data if isinstance(data, list) else [])
        return self.import_codes(codes, source_file=path.name)

    def claim_code(self, user_key: str, user_name: str = "", message_text: str = "", message_id: str = "", now: Optional[datetime] = None) -> ClaimResult:
        if not user_key:
            return ClaimResult(status="invalid_user")
        now = now or datetime.now(TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ)
        claim_date = now.astimezone(timezone.utc).date().isoformat()
        now_iso = now.astimezone(TZ).isoformat(timespec="seconds")
        now_ts = int(now.timestamp())
        con = self.connect()
        try:
            con.execute("begin immediate")
            existing = con.execute("""
                select cr.code, cr.quota, cr.display_amount, cr.display_unit, dc.expired_at, cr.created_at
                from claim_records cr left join daily_codes dc on dc.code = cr.code
                where cr.user_key = ? and cr.claim_date = ? and cr.status = 'success'
            """, (user_key, claim_date)).fetchone()
            if existing:
                con.commit()
                return ClaimResult("already_claimed", existing["code"], existing["quota"], existing["display_amount"], existing["display_unit"], existing["expired_at"], existing["created_at"])
            code = con.execute("""
                select * from daily_codes
                where batch_date = ? and status = 'unused' and expired_at > ?
                order by id asc limit 1
            """, (claim_date, now_ts)).fetchone()
            if not code:
                con.commit()
                return ClaimResult(status="no_code")
            con.execute("update daily_codes set status='claimed', claimed_by=?, claimed_name=?, claimed_at=? where id=? and status='unused'", (user_key, user_name, now_iso, code["id"]))
            con.execute("""
                insert into claim_records
                (user_key, user_name, claim_date, code, quota, display_amount, display_unit, message_text, message_id, created_at, status)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success')
            """, (user_key, user_name, claim_date, code["code"], code["quota"], code["display_amount"], code["display_unit"], message_text, message_id, now_iso))
            con.commit()
            return ClaimResult("success", code["code"], code["quota"], code["display_amount"], code["display_unit"], code["expired_at"], now_iso)
        except sqlite3.IntegrityError:
            con.rollback()
            return self.claim_code(user_key, user_name, message_text, message_id, now=now)
        finally:
            con.close()
