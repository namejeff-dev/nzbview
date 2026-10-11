#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from config import config_path, database_path, read_config, placeholder


def main() -> None:
    parser = argparse.ArgumentParser(description="nzbview v2.15.2")
    parser.add_argument("--db", help="Existing SQLite DB, or the path for a new database")
    parser.add_argument("--collector-config", default=str(config_path()))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true", help="Development only")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--init-db", action="store_true", help="Initialize an empty DB without contacting services")
    args = parser.parse_args()
    token = os.environ.get("NZBVIEW_ACCESS_TOKEN", "")
    if token and (len(token) < 24 or placeholder(token)):
        parser.error("NZBVIEW_ACCESS_TOKEN must be a generated token with at least 24 characters.")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not token:
        parser.error("Set NZBVIEW_ACCESS_TOKEN before binding to a network interface.")
    path = Path(args.collector_config).expanduser().resolve()
    try:
        db = database_path(path, args.db)
        cfg = read_config(path) if path.is_file() else {}
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    os.environ["SABNZBD_DB"] = str(db)
    os.environ["NZBVIEW_COLLECTOR_CONFIG"] = str(path)
    os.environ["NZBVIEW_LOG_TIMEZONE"] = str(cfg.get("log_timezone") or os.environ.get("NZBVIEW_LOG_TIMEZONE") or "")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        if os.environ["NZBVIEW_LOG_TIMEZONE"]:
            ZoneInfo(os.environ["NZBVIEW_LOG_TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        parser.error("Unknown log_timezone; install dependencies and use an IANA timezone name.")
    if args.init_db:
        from runtime import collection_lock
        from database_collector import open_db
        try:
            with collection_lock(db):
                conn = open_db(db)
                conn.commit()
                conn.close()
        except (OSError, RuntimeError) as exc:
            parser.error(str(exc))
    print("nzbview v2.15.2")
    print(f"Database: {db}")
    print(f"Config: {path}")
    print(f"Open: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}")
    uvicorn.run("app:app", app_dir=str(Path(__file__).parent), host=args.host, port=args.port,
                reload=args.reload, access_log=not args.quiet, proxy_headers=False,
                log_level="warning" if args.quiet else "info")


if __name__ == "__main__":
    main()
