"""Run one collection or maintenance operation using the shared config."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from config import collector_command, config_path, database_path, load_config


def main(mode: str | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect nzbview data from the configured services and logs.")
    parser.add_argument("--collector-config", default=str(config_path()))
    parser.add_argument("--db", help="Must match db in the config when that field is present")
    args = parser.parse_args()
    try:
        path = Path(args.collector_config).expanduser().resolve()
        db = database_path(path, args.db)
        cfg = load_config(path, db, maintenance=mode is not None)
        cmd, env = collector_command(cfg, mode=mode)
        return subprocess.call(cmd, cwd=str(Path(cfg["script"]).parent), env=env)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
