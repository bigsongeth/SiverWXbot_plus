"""Pull today's wechat-checkin codes from hkbohai and import into local SQLite.

Counterpart of hkbohai:/root/new-api/scripts/run_daily_checkin.sh (cron, daily 08:00
Beijing). This side runs on win-shukong via scheduled task 'WechatCheckinPull'
(daily 08:05). Manual run from project root:

    python -m plugins.wechat_checkin.pull_and_import

Requires: C:\\Users\\Admin\\.ssh\\config entry 'hkbohai' with key auth (id_ed25519).
Output is ASCII-only on purpose: the GBK console / scheduled-task log chokes on
non-ASCII. hkbohai drops rapid repeat SSH connections, hence the retry loop.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

try:
    from .store import CheckinStore
    from .handler import DB_PATH
except ImportError:
    from store import CheckinStore
    from handler import DB_PATH

PLUGIN_DIR = Path(__file__).resolve().parent
IMPORTS_DIR = PLUGIN_DIR / "imports"
SCP = r"C:\Windows\System32\OpenSSH\scp.exe"
REMOTE = "hkbohai:/root/new-api/data/wechat_checkin_exports/latest.json"
ATTEMPTS = 3
RETRY_WAIT_S = 30


def pull(tmp: Path) -> None:
    last_err = ""
    for attempt in range(1, ATTEMPTS + 1):
        proc = subprocess.run(
            [SCP, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", REMOTE, str(tmp)],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 0:
            return
        last_err = proc.stderr.strip()
        print(f"scp attempt {attempt}/{ATTEMPTS} failed rc={proc.returncode}: {last_err}")
        if attempt < ATTEMPTS:
            time.sleep(RETRY_WAIT_S)
    raise RuntimeError(f"scp failed after {ATTEMPTS} attempts: {last_err}")


def main() -> int:
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = IMPORTS_DIR / "_latest_pull.json"
    pull(tmp)
    data = json.loads(tmp.read_text(encoding="utf-8"))
    batch_date = data.get("batch_date", "unknown")
    target = IMPORTS_DIR / f"wechat-checkin-{batch_date}-pull.json"
    tmp.replace(target)
    count = CheckinStore(Path(DB_PATH)).import_json_file(str(target))
    print(f"ok batch_date={batch_date} imported={count} file={target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
