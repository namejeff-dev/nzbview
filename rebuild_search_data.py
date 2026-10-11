"""Rebuild derived search evidence while preserving history/provider data."""
from collect import main

if __name__ == "__main__":
    raise SystemExit(main("--rebuild-search-layer"))
