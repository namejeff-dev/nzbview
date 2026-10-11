"""Shared, portable configuration for the dashboard and collector commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / "collector_config.json"
SECRET_ENV = {
    "sab_api_key": "SABNZBD_API_KEY",
    "prowlarr_api_key": "PROWLARR_API_KEY",
    "sonarr_api_key": "SONARR_API_KEY",
    "radarr_api_key": "RADARR_API_KEY",
}


def placeholder(value: object) -> bool:
    text = str(value or "").strip().upper()
    return (not text or text.startswith(("YOUR_", "REPLACE_", "CHANGE_ME", "CHANGEME"))
            or text in {"API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD"})


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(os.path.expandvars(str(value))).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def config_path() -> Path:
    return Path(os.environ.get("NZBVIEW_COLLECTOR_CONFIG", DEFAULT_CONFIG)).expanduser().resolve()


def read_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"Copy collector_config.example.json to {path.name} and configure it.") from None
    if not isinstance(data, dict):
        raise ValueError("Collector config must be a JSON object.")
    return data


def database_path(path: Path | None = None, override: str | None = None) -> Path:
    path = path or config_path()
    explicit = override or os.environ.get("SABNZBD_DB")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if path.is_file():
        data = read_config(path)
        if data.get("db"):
            return resolve_path(data["db"], path.parent)
    return (path.parent / "data" / "database_collector.db").resolve()


def load_config(path: Path, db: Path | None = None, *, maintenance: bool = False) -> dict:
    data = read_config(path)
    cfg = dict(data)
    cfg["script"] = str(resolve_path(data.get("script") or "database_collector.py", path.parent))
    if not Path(cfg["script"]).is_file():
        raise ValueError(f"Collector script not found: {cfg['script']}")
    configured_db = database_path(path, override=str(db) if db is not None else None)
    if db is not None and data.get("db") and resolve_path(data["db"], path.parent) != db.resolve():
        raise ValueError("Collector config DB does not match the dashboard DB. Use the same path.")
    cfg["db"] = str(configured_db)
    for prefix in ("sab", "prowlarr", "sonarr", "radarr"):
        secret = f"{prefix}_api_key"
        cfg[secret] = os.environ.get(SECRET_ENV[secret], str(data.get(secret) or "")).strip()
        url_key = f"{prefix}_url"
        url = str(data.get(url_key) or "").strip()
        required = prefix in {"sab", "prowlarr"} and not maintenance
        if required or (not maintenance and not placeholder(cfg[secret])):
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError(f"{url_key} must be an http(s) URL without embedded credentials.")
            try:
                parts.port
            except ValueError:
                raise ValueError(f"Invalid port in {url_key}.") from None
        if required and placeholder(cfg[secret]):
            raise ValueError(f"Configure {secret} or {SECRET_ENV[secret]}; placeholders are not valid keys.")
        cfg[url_key] = url
        if prefix in {"sonarr", "radarr"} and placeholder(cfg[secret]):
            cfg[secret] = ""
        selected = data.get(f"{prefix}_log_dir") or data.get(f"{prefix}_log")
        if not selected:
            if prefix in {"sab", "prowlarr"}:
                raise ValueError(f"Configure {prefix}_log_dir (or {prefix}_log).")
            continue
        key = f"{prefix}_log_dir" if data.get(f"{prefix}_log_dir") else f"{prefix}_log"
        cfg[key] = str(resolve_path(selected, path.parent))
        source = Path(cfg[key])
        if not source.exists() or (key.endswith("_dir") and not source.is_dir()):
            raise ValueError(f"Log path not found: {cfg[key]}")
    for key, default, minimum, maximum in (
        ("match_window_hours", 168, 1, 1440), ("timeout_seconds", 900, 1, 86400),
    ):
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}.")
        cfg[key] = value
    zone = str(data.get("log_timezone") or os.environ.get("NZBVIEW_LOG_TIMEZONE") or "").strip()
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown log_timezone: {zone}. Use an IANA name such as Europe/Berlin.") from None
    cfg["log_timezone"] = zone
    extra = data.get("extra_args") or []
    if not isinstance(extra, list) or any(x != "--debug-prowlarr" for x in extra):
        raise ValueError("extra_args may contain only --debug-prowlarr; configure other settings explicitly.")
    cfg["extra_args"] = extra
    return cfg


def collector_command(cfg: dict, *, mode: str | None = None) -> tuple[list[str], dict[str, str]]:
    import sys
    cmd = [sys.executable, cfg["script"], "--db", cfg["db"],
           "--match-window-hours", str(cfg["match_window_hours"])]
    if mode:
        cmd.append(mode)
    else:
        cmd += ["--url", cfg["sab_url"], "--prowlarr-url", cfg["prowlarr_url"]]
    for prefix in ("sab", "prowlarr", "sonarr", "radarr"):
        flag = "log" if prefix == "sab" else f"{prefix}-log"
        if cfg.get(f"{prefix}_log_dir"):
            cmd += [f"--{flag}-dir", cfg[f"{prefix}_log_dir"]]
        elif cfg.get(f"{prefix}_log"):
            cmd += [f"--{flag}", cfg[f"{prefix}_log"]]
        if prefix in {"sonarr", "radarr"} and cfg.get(f"{prefix}_api_key") and not mode:
            cmd += [f"--{prefix}-url", cfg[f"{prefix}_url"]]
    cmd += cfg.get("extra_args", [])
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NZBVIEW_LOG_TIMEZONE"] = cfg.get("log_timezone", "")
    for key, name in SECRET_ENV.items():
        env[name] = cfg.get(key, "")
    return cmd, env
