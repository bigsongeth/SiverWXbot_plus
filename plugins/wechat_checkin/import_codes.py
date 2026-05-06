from __future__ import annotations
import argparse
from pathlib import Path
try:
    from .store import CheckinStore
    from .handler import DB_PATH
except ImportError:
    from store import CheckinStore
    from handler import DB_PATH

def main():
    parser = argparse.ArgumentParser(description="Import wechat checkin redemption codes into local SQLite.")
    parser.add_argument("json_file", help="JSON exported by new-api generator")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite DB path")
    args = parser.parse_args()
    count = CheckinStore(Path(args.db)).import_json_file(args.json_file)
    print(f"imported={count} db={args.db}")

if __name__ == "__main__":
    main()
