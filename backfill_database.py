"""Replay retained logs with a consistent SQLite backup and collector lock."""
from collect import main

if __name__ == "__main__":
    raise SystemExit(main("--backfill-all"))
