#!/usr/bin/env python3
r"""
nzbview Database Collector v7.11.0

Combines:
- SABnzbd server_stats API
- Rotating SABnzbd log-family parsing with lifecycle/failure reconstruction
- Prowlarr v1 API (Usenet/NZB indexers only)
- Rotating Prowlarr log-family parsing
- Rotating Sonarr/Radarr log-family parsing for search/grab/import orchestration
- SQLite persistence
- Conservative Arr/Prowlarr/SAB correlation by exact release title, episode identity, media title and time

No third-party packages required.

Example (PowerShell):
    python .\database_collector.py `
      --url http://localhost:8085 `
      --api-key YOUR_SAB_API_KEY `
      --log-dir "C:\Users\YOUR_USER\AppData\Local\sabnzbd\logs" `
      --prowlarr-url http://localhost:9696 `
      --prowlarr-api-key YOUR_PROWLARR_API_KEY `
      --prowlarr-log-dir "D:\Services\prowlarr\logs" `
      --sonarr-log-dir "D:\Services\sonarr\logs" `
      --radarr-log-dir "D:\Services\radarr\logs"

Environment variables:
    SABNZBD_URL
    SABNZBD_API_KEY
    SABNZBD_LOG
    SABNZBD_LOG_DIR
    SABNZBD_DB
    PROWLARR_URL
    PROWLARR_API_KEY
    PROWLARR_LOG
    PROWLARR_LOG_DIR
    SONARR_LOG
    SONARR_LOG_DIR
    RADARR_LOG
    RADARR_LOG_DIR
    SONARR_URL
    SONARR_API_KEY
    RADARR_URL
    RADARR_API_KEY
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import BASE, config_path, database_path, placeholder
from runtime import collection_lock, complete_lines, local_now, localize, redact

VERSION = "7.11.0"


# ===========================================================================
# Generic helpers
# ===========================================================================

def now_iso() -> str:
    return local_now().isoformat(timespec="milliseconds")


def pct(numer: int, denom: int) -> str:
    return "n/a" if denom <= 0 else f"{numer / denom * 100.0:.2f}%"


def human_bytes(value: int | float) -> str:
    value = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} PB"


def numeric_total(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return 0
        try:
            return int(float(v))
        except ValueError:
            return 0
    if isinstance(value, dict):
        return sum(numeric_total(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(numeric_total(v) for v in value)
    return 0


def normalized_release_identity(name: str) -> str:
    """Conservative identity key for counting unique SAB releases.

    Preserve punctuation differences except whitespace normalization so we do
    not accidentally merge distinct release names.
    """
    return " ".join(name.strip().lower().split())


def normalized_match_title(name: str) -> str:
    """Looser key used only when matching Prowlarr titles to SAB job names."""
    name = name.strip().lower()
    name = re.sub(r"[\s._-]+", " ", name)
    name = re.sub(r"[^a-z0-9 ]+", "", name)
    return " ".join(name.split())


def stable_hash(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def parse_timestamp_any(value: Any) -> str | None:
    """Best-effort conversion of ISO-ish API timestamps into ISO strings."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Prowlarr timestamps are normally ISO 8601. Preserve them if parseable.
    try:
        fixed = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(fixed)
        return localize(dt).isoformat(timespec="milliseconds")
    except ValueError:
        pass

    # SAB/Prowlarr text-log formats.
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(s, fmt)
            return localize(dt).isoformat(timespec="milliseconds")
        except ValueError:
            pass
    return None


# ===========================================================================
# SABnzbd
# ===========================================================================

@dataclass
class ProviderStats:
    name: str
    label: str | None
    tried: int
    success: int
    day_bytes: int
    week_bytes: int
    month_bytes: int
    total_bytes: int
    priority: int | None = None
    connections: int | None = None
    enabled: bool | None = None

    @property
    def availability(self) -> float | None:
        # SAB's Config -> Servers availability display is based on the
        # requested-article counter minus the API's articles_success counter.
        # Keep this aligned with SAB's own UI rather than treating the raw API
        # field name as a release-completion metric.
        return None if self.tried <= 0 else max(0, self.tried - self.success) / self.tried * 100.0

    @property
    def unavailable(self) -> int:
        return max(0, self.success)

    @property
    def available(self) -> int:
        return max(0, self.tried - self.success)


def sab_api_get(base_url: str, api_key: str, mode: str, **params: Any) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    api_url = base_url if base_url.endswith("/api") else f"{base_url}/api"
    query = {
        "mode": mode,
        "output": "json",
        "apikey": api_key,
        **{k: v for k, v in params.items() if v is not None},
    }
    url = f"{api_url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"sab-prowlarr-monitor/{VERSION}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"SABnzbd HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to SABnzbd: {exc.reason}") from exc
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"SABnzbd API error: {data['error']}")
    return data


def sab_version(base_url: str) -> str:
    try:
        base_url = base_url.rstrip("/")
        api_url = base_url if base_url.endswith("/api") else f"{base_url}/api"
        with urllib.request.urlopen(f"{api_url}?mode=version&output=json", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return str(data.get("version", "unknown"))
    except Exception:
        return "unknown"



def _sab_completed_iso(value: Any) -> str | None:
    """Convert SAB history `completed` unix timestamps to local ISO strings."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return parse_timestamp_any(value)


def fetch_sab_history(
    base_url: str,
    api_key: str,
    page_size: int = 500,
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Fetch normal and archived SAB history, including fail_message/stage_log.

    SAB's history API exposes the post-processing stage log and failure message
    per history slot.  Retried items can move into the archive, so both views
    are collected and de-duplicated by nzo_id (or a stable fallback key).
    """
    merged: dict[str, dict[str, Any]] = {}
    for archive in (0, 1):
        start = 0
        while start < max_rows:
            payload = sab_api_get(
                base_url, api_key, "history",
                start=start, limit=min(page_size, max_rows - start), archive=archive,
            )
            history = payload.get("history") if isinstance(payload, dict) else None
            if not isinstance(history, dict):
                break
            rows = history.get("slots") or []
            if not isinstance(rows, list) or not rows:
                break
            for rec in rows:
                if not isinstance(rec, dict):
                    continue
                nzo_id = str(rec.get("nzo_id") or "").strip()
                completed = rec.get("completed")
                name = str(rec.get("name") or rec.get("nzb_name") or "").strip()
                key = nzo_id or stable_hash(name, completed, rec.get("status"), archive)
                row = dict(rec)
                row["_archive_view"] = archive
                # Prefer a record that has richer diagnostic fields.
                old = merged.get(key)
                old_score = 0 if old is None else int(bool(old.get("fail_message"))) + int(bool(old.get("stage_log")))
                new_score = int(bool(row.get("fail_message"))) + int(bool(row.get("stage_log")))
                if old is None or new_score >= old_score:
                    merged[key] = row
            start += len(rows)
            noof = history.get("noofslots")
            try:
                if noof is not None and start >= int(noof):
                    break
            except (TypeError, ValueError):
                pass
            if len(rows) < page_size:
                break
    return list(merged.values())


def import_sab_history(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    imported = 0
    stamp = now_iso()
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("name") or rec.get("nzb_name") or "").strip()
        if not name:
            continue
        nzo_id = str(rec.get("nzo_id") or "").strip()
        completed_raw = rec.get("completed")
        history_id = nzo_id or stable_hash(name, completed_raw, rec.get("status"), rec.get("_archive_view", 0))
        stage_log = rec.get("stage_log")
        if not isinstance(stage_log, list):
            stage_log = []
        fail_message = str(rec.get("fail_message") or "").strip()
        status = str(rec.get("status") or "").strip()
        completed_time = _sab_completed_iso(completed_raw)
        try:
            download_time = int(rec.get("download_time") or 0)
        except (TypeError, ValueError):
            download_time = 0
        try:
            postproc_time = int(rec.get("postproc_time") or 0)
        except (TypeError, ValueError):
            postproc_time = 0
        cur = conn.execute(
            """
            INSERT INTO sab_history(
                history_id,nzo_id,completed_time,job_name,status,fail_message,
                stage_log_json,download_time,postproc_time,archive,imported_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(history_id) DO UPDATE SET
                nzo_id=excluded.nzo_id,
                completed_time=COALESCE(excluded.completed_time,sab_history.completed_time),
                job_name=excluded.job_name,
                status=excluded.status,
                fail_message=CASE WHEN excluded.fail_message<>'' THEN excluded.fail_message ELSE sab_history.fail_message END,
                stage_log_json=CASE WHEN excluded.stage_log_json<>'[]' THEN excluded.stage_log_json ELSE sab_history.stage_log_json END,
                download_time=excluded.download_time,
                postproc_time=excluded.postproc_time,
                archive=excluded.archive,
                updated_at=excluded.updated_at
            """,
            (
                history_id, nzo_id or None, completed_time, name, status, fail_message,
                json.dumps(stage_log, ensure_ascii=False), download_time, postproc_time,
                int(bool(rec.get("archive") or rec.get("_archive_view"))), stamp, stamp,
            ),
        )
        if cur.rowcount > 0:
            imported += 1
    return imported

def sab_server_config_map(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Normalize SAB ``get_config&section=servers`` into a hostname/name map.

    SAB versions/plugins have returned the server section in a few wrapper
    shapes (list, dict keyed by server name, or nested config objects). Walk the
    structure generically and retain only non-sensitive role/display metadata.
    """
    if not isinstance(payload, dict):
        return {}

    def candidate_rows(node: Any, hint: str | None = None):
        if isinstance(node, list):
            for item in node:
                yield from candidate_rows(item, hint)
            return
        if not isinstance(node, dict):
            return

        keys = {str(k).lower() for k in node.keys()}
        serverish = bool(keys & {"host", "name", "displayname", "priority", "connections", "enable", "enabled"})
        if serverish and ("host" in keys or "name" in keys or hint):
            yield hint, node

        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from candidate_rows(value, str(key))

    out: dict[str, dict[str, Any]] = {}
    for hint, item in candidate_rows(payload):
        host = str(item.get("host") or "").strip()
        name = str(item.get("name") or hint or "").strip()
        display = str(item.get("displayname") or item.get("display_name") or "").strip()
        names = {host.lower(), name.lower(), display.lower()}
        names.discard("")
        if not names:
            continue
        try:
            priority = int(item.get("priority")) if item.get("priority") not in (None, "") else None
        except (TypeError, ValueError):
            priority = None
        try:
            connections = int(item.get("connections")) if item.get("connections") not in (None, "") else None
        except (TypeError, ValueError):
            connections = None
        enable_raw = item.get("enable", item.get("enabled"))
        if enable_raw in (None, ""):
            enabled = None
        elif isinstance(enable_raw, bool):
            enabled = enable_raw
        else:
            enable_text = str(enable_raw).strip().lower()
            if enable_text in {"1", "true", "yes", "on", "enabled"}:
                enabled = True
            elif enable_text in {"0", "false", "no", "off", "disabled"}:
                enabled = False
            else:
                enabled = None

        # A configured display name is best. The server's configured name is
        # second-best. Only fall back to the host when SAB exposes nothing else.
        display_label = display or name or host or None
        data = {"priority": priority, "connections": connections, "enabled": enabled, "label": display_label}
        for lookup in names:
            out[lookup] = data
    return out


def sab_provider_report(stats: dict[str, Any], server_config: dict[str, dict[str, Any]] | None = None) -> list[ProviderStats]:
    out = []
    server_config = server_config or {}
    for name, item in (stats.get("servers") or {}).items():
        if not isinstance(item, dict):
            continue
        cfg = server_config.get(str(name).strip().lower(), {})
        out.append(
            ProviderStats(
                name=name,
                label=cfg.get("label") or str(name),
                tried=numeric_total(item.get("articles_tried")),
                success=numeric_total(item.get("articles_success")),
                day_bytes=numeric_total(item.get("day")),
                week_bytes=numeric_total(item.get("week")),
                month_bytes=numeric_total(item.get("month")),
                total_bytes=numeric_total(item.get("total")),
                priority=cfg.get("priority"),
                connections=cfg.get("connections"),
                enabled=cfg.get("enabled"),
            )
        )
    return out


# ===========================================================================
# Prowlarr API
# ===========================================================================

def prowlarr_api_get(base_url: str, api_key: str, path: str, **params: Any) -> Any:
    base_url = base_url.rstrip("/")
    path = path if path.startswith("/") else "/" + path
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base_url}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": f"sab-prowlarr-monitor/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Prowlarr HTTP {exc.code} for {path}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to Prowlarr: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Prowlarr returned non-JSON for {path}") from exc


def arr_api_get(app: str, base_url: str, api_key: str, path: str, **params: Any) -> Any:
    """Small Sonarr/Radarr v3 GET helper used only for quality-profile snapshots.

    Arr API access is optional. Log collection continues when these endpoints are
    not configured; raw search candidates are still retained, but target-quality
    scoring remains unresolved until a profile snapshot is available.
    """
    app = str(app or "arr").strip().title()
    base_url = str(base_url or "").rstrip("/")
    path = path if path.startswith("/") else "/" + path
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{base_url}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": f"nzbview-collector/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{app} HTTP {exc.code} for {path}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to {app}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{app} returned non-JSON for {path}") from exc


def prowlarr_version(base_url: str, api_key: str) -> str:
    try:
        data = prowlarr_api_get(base_url, api_key, "/api/v1/system/status")
        if isinstance(data, dict):
            return str(data.get("version", "unknown"))
    except Exception:
        pass
    return "unknown"


def is_usenet_indexer(item: dict[str, Any]) -> bool:
    protocol = str(item.get("protocol", "")).strip().lower()
    # Current Prowlarr uses "usenet"; tolerate newznab-ish variants.
    return protocol in {"usenet", "nzb", "newznab"}


def load_usenet_indexers(base_url: str, api_key: str) -> dict[int, dict[str, Any]]:
    raw = prowlarr_api_get(base_url, api_key, "/api/v1/indexer")
    result: dict[int, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return result

    for item in raw:
        if not isinstance(item, dict) or not is_usenet_indexer(item):
            continue
        try:
            idx = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        result[idx] = item
    return result


def extract_stats_rows(payload: Any) -> list[dict[str, Any]]:
    """Handle both common IndexerStats response shapes."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("indexers", "indexerStats", "stats", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def stat_int(item: dict[str, Any], *names: str) -> int:
    for n in names:
        if n in item:
            return numeric_total(item.get(n))
    return 0


def stat_float(item: dict[str, Any], *names: str) -> float:
    for n in names:
        if n in item and item.get(n) is not None:
            try:
                return float(item[n])
            except (TypeError, ValueError):
                pass
    return 0.0


def extract_history_records(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], None
    if not isinstance(payload, dict):
        return [], None

    for key in ("records", "items", "history"):
        rows = payload.get(key)
        if isinstance(rows, list):
            total = payload.get("totalRecords", payload.get("total"))
            try:
                total_int = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_int = None
            return [x for x in rows if isinstance(x, dict)], total_int
    return [], None


def history_event_type(record: dict[str, Any]) -> str:
    raw = record.get("eventType", record.get("eventTypeName", record.get("type", "")))
    # Recent Prowlarr JSON serializers normally expose enum names as strings.
    if isinstance(raw, str):
        return raw.strip().lower()
    # Keep numeric enum values explicit instead of guessing their meaning.
    return f"enum:{raw}" if raw is not None else "unknown"


def history_indexer_id(record: dict[str, Any]) -> int | None:
    for key in ("indexerId", "indexer_id"):
        try:
            if record.get(key) is not None:
                return int(record[key])
        except (TypeError, ValueError):
            pass
    data = record.get("data")
    if isinstance(data, dict):
        try:
            if data.get("indexerId") is not None:
                return int(data["indexerId"])
        except (TypeError, ValueError):
            pass
    return None


def history_title(record: dict[str, Any]) -> str:
    """Extract a release title from Prowlarr history across version variants."""
    candidate_keys = (
        "releaseTitle", "grabTitle", "title", "sourceTitle", "nzbTitle",
        "downloadTitle", "releaseName"
    )

    for key in candidate_keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    def walk(obj: Any, depth: int = 0) -> str:
        if depth > 3:
            return ""
        if isinstance(obj, dict):
            # Prefer exact release/title-ish keys.
            lower_map = {str(k).lower(): k for k in obj.keys()}
            for wanted in (
                "releasetitle", "grabtitle", "sourcetitle", "nzbtitle",
                "downloadtitle", "releasename", "title"
            ):
                real_key = lower_map.get(wanted)
                if real_key is not None:
                    value = obj.get(real_key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            # Then recurse into nested dict/list values.
            for value in obj.values():
                found = walk(value, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj[:20]:
                found = walk(value, depth + 1)
                if found:
                    return found
        return ""

    return walk(record.get("data"))


def history_date(record: dict[str, Any]) -> str:
    for key in ("date", "eventTime", "timestamp"):
        ts = parse_timestamp_any(record.get(key))
        if ts:
            return ts
    return now_iso()


def fetch_prowlarr_history(
    base_url: str,
    api_key: str,
    max_pages: int = 50,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch retained Prowlarr history, newest first.

    Uses pagination and stops when a short page is returned or totalRecords is met.
    """
    all_rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = prowlarr_api_get(
            base_url,
            api_key,
            "/api/v1/history",
            page=page,
            pageSize=page_size,
            sortKey="date",
            sortDirection="descending",
        )
        rows, total = extract_history_records(payload)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        if total is not None and len(all_rows) >= total:
            break
    return all_rows


# ===========================================================================
# SQLite
# ===========================================================================

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS provider_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_label TEXT,
    articles_tried INTEGER NOT NULL,
    articles_success INTEGER NOT NULL,
    day_bytes INTEGER NOT NULL,
    week_bytes INTEGER NOT NULL,
    month_bytes INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    priority INTEGER,
    connections INTEGER,
    enabled INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,
    max_repair_blocks INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_time ON jobs(event_time);
CREATE INDEX IF NOT EXISTS idx_jobs_name ON jobs(job_name);

CREATE TABLE IF NOT EXISTS sab_history (
    history_id TEXT PRIMARY KEY,
    nzo_id TEXT,
    completed_time TEXT,
    job_name TEXT NOT NULL,
    status TEXT,
    fail_message TEXT,
    stage_log_json TEXT,
    download_time INTEGER NOT NULL DEFAULT 0,
    postproc_time INTEGER NOT NULL DEFAULT 0,
    archive INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sab_history_name ON sab_history(job_name);
CREATE INDEX IF NOT EXISTS idx_sab_history_time ON sab_history(completed_time);

CREATE TABLE IF NOT EXISTS sab_log_segments (
    segment_id TEXT PRIMARY KEY,
    first_time TEXT,
    first_line_hash TEXT NOT NULL,
    current_path TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sab_log_segments_time ON sab_log_segments(first_time);

CREATE TABLE IF NOT EXISTS sab_log_events (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    job_name TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT,
    details_json TEXT,
    segment_id TEXT,
    raw_line TEXT,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sab_log_events_time ON sab_log_events(event_time);
CREATE INDEX IF NOT EXISTS idx_sab_log_events_job ON sab_log_events(job_name, event_time);
CREATE INDEX IF NOT EXISTS idx_sab_log_events_kind ON sab_log_events(kind, event_time);

CREATE TABLE IF NOT EXISTS sab_log_family_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    root_path TEXT NOT NULL,
    segments_found INTEGER NOT NULL DEFAULT 0,
    segments_read INTEGER NOT NULL DEFAULT 0,
    new_segments INTEGER NOT NULL DEFAULT 0,
    lines_read INTEGER NOT NULL DEFAULT 0,
    unavailable_events INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    repair_events INTEGER NOT NULL DEFAULT 0,
    lifecycle_events INTEGER NOT NULL DEFAULT 0,
    live_completed INTEGER NOT NULL DEFAULT 0,
    backfill_completed INTEGER NOT NULL DEFAULT 0,
    live_failed INTEGER NOT NULL DEFAULT 0,
    backfill_failed INTEGER NOT NULL DEFAULT 0,
    live_cancelled INTEGER NOT NULL DEFAULT 0,
    backfill_cancelled INTEGER NOT NULL DEFAULT 0,
    live_lifecycle_events INTEGER NOT NULL DEFAULT 0,
    backfill_lifecycle_events INTEGER NOT NULL DEFAULT 0,
    reset_detected INTEGER NOT NULL DEFAULT 0,
    continuity_status TEXT NOT NULL DEFAULT 'unknown',
    continuity_message TEXT,
    continuity_overlap INTEGER NOT NULL DEFAULT 0,
    oldest_first_time TEXT,
    newest_first_time TEXT
);

CREATE TABLE IF NOT EXISTS sab_attempt_diagnostics (
    event_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    final_time TEXT NOT NULL,
    final_status TEXT NOT NULL,
    failure_stage TEXT,
    failure_code TEXT,
    failure_confidence TEXT,
    failure_reason TEXT,
    lifecycle_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'log',
    history_id TEXT,
    log_event_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sab_attempt_diag_time ON sab_attempt_diagnostics(final_time);
CREATE INDEX IF NOT EXISTS idx_sab_attempt_diag_failure ON sab_attempt_diagnostics(failure_stage, final_time);

CREATE TABLE IF NOT EXISTS repair_state (
    job_name TEXT PRIMARY KEY,
    max_repair_blocks INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS log_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    log_path TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    lines_read INTEGER NOT NULL,
    unavailable_events INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    repair_events INTEGER NOT NULL,
    jobs_with_repair_activity INTEGER NOT NULL,
    reset_detected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS log_state (
    log_path TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prowlarr_indexers (
    indexer_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    protocol TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    priority INTEGER,
    privacy TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prowlarr_stats_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    indexer_id INTEGER NOT NULL,
    indexer_name TEXT NOT NULL,
    queries INTEGER NOT NULL DEFAULT 0,
    grabs INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    avg_response_ms REAL NOT NULL DEFAULT 0,
    failed_queries INTEGER NOT NULL DEFAULT 0,
    failed_grabs INTEGER NOT NULL DEFAULT 0,
    failed_rss INTEGER NOT NULL DEFAULT 0,
    avg_grab_response_ms REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_prowlarr_stats_time
ON prowlarr_stats_snapshots(collected_at);

CREATE TABLE IF NOT EXISTS prowlarr_history (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    indexer_id INTEGER,
    indexer_name TEXT,
    event_type TEXT NOT NULL,
    release_title TEXT,
    data_json TEXT,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prowlarr_hist_time
ON prowlarr_history(event_time);

CREATE INDEX IF NOT EXISTS idx_prowlarr_hist_indexer
ON prowlarr_history(indexer_id, event_time);

CREATE TABLE IF NOT EXISTS prowlarr_log_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    log_path TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    lines_read INTEGER NOT NULL,
    searches INTEGER NOT NULL,
    connection_failures INTEGER NOT NULL,
    invalid_releases INTEGER NOT NULL,
    warnings INTEGER NOT NULL,
    reset_detected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prowlarr_log_searches (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    indexer_name TEXT NOT NULL,
    query_text TEXT,
    query_signature TEXT,
    result_count INTEGER,
    raw_line TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prowlarr_log_errors (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    indexer_name TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prowlarr_log_segments (
    segment_id TEXT PRIMARY KEY,
    first_time TEXT,
    first_line_hash TEXT NOT NULL,
    current_path TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prowlarr_log_segments_time
ON prowlarr_log_segments(first_time);

CREATE TABLE IF NOT EXISTS prowlarr_log_family_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    root_path TEXT NOT NULL,
    segments_found INTEGER NOT NULL DEFAULT 0,
    segments_read INTEGER NOT NULL DEFAULT 0,
    new_segments INTEGER NOT NULL DEFAULT 0,
    lines_read INTEGER NOT NULL DEFAULT 0,
    searches INTEGER NOT NULL DEFAULT 0,
    connection_failures INTEGER NOT NULL DEFAULT 0,
    invalid_releases INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    live_searches INTEGER NOT NULL DEFAULT 0,
    backfill_searches INTEGER NOT NULL DEFAULT 0,
    live_diagnostics INTEGER NOT NULL DEFAULT 0,
    backfill_diagnostics INTEGER NOT NULL DEFAULT 0,
    reset_detected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS arr_log_segments (
    app TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    first_time TEXT,
    first_line_hash TEXT NOT NULL,
    current_path TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(app, segment_id)
);

CREATE INDEX IF NOT EXISTS idx_arr_log_segments_time
ON arr_log_segments(app, first_time);

CREATE TABLE IF NOT EXISTS arr_events (
    event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    app TEXT NOT NULL,
    component TEXT,
    event_type TEXT NOT NULL,
    search_mode TEXT,
    origin_confidence TEXT,
    media_title TEXT,
    season INTEGER,
    episode INTEGER,
    release_name TEXT,
    indexer TEXT,
    download_client TEXT,
    severity TEXT,
    reason TEXT,
    message TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    segment_id TEXT,
    raw_line TEXT,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arr_events_time
ON arr_events(app, event_time);

CREATE INDEX IF NOT EXISTS idx_arr_events_release
ON arr_events(release_name, event_time);

CREATE INDEX IF NOT EXISTS idx_arr_events_media
ON arr_events(media_title, event_time);

CREATE INDEX IF NOT EXISTS idx_arr_events_type
ON arr_events(event_type, event_time);

CREATE TABLE IF NOT EXISTS arr_search_candidates (
    candidate_id TEXT PRIMARY KEY,
    search_event_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    app TEXT NOT NULL,
    media_title TEXT,
    season INTEGER,
    episode INTEGER,
    indexer TEXT NOT NULL,
    release_name TEXT NOT NULL,
    quality_raw TEXT,
    quality_name TEXT,
    quality_tier TEXT,
    parsed_media_title TEXT,
    parsed_season INTEGER,
    parsed_episode INTEGER,
    media_match INTEGER,
    decision TEXT NOT NULL DEFAULT 'considered',
    rejection_reasons TEXT,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arr_search_candidates_search
ON arr_search_candidates(search_event_id, indexer);

CREATE INDEX IF NOT EXISTS idx_arr_search_candidates_quality
ON arr_search_candidates(quality_tier, event_time);

CREATE INDEX IF NOT EXISTS idx_arr_search_candidates_time
ON arr_search_candidates(event_time);

CREATE TABLE IF NOT EXISTS arr_search_targets (
    search_event_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    app TEXT NOT NULL,
    media_title TEXT,
    season INTEGER,
    episode INTEGER,
    profile_id INTEGER,
    profile_name TEXT,
    cutoff_id TEXT,
    target_label TEXT NOT NULL,
    target_qualities_json TEXT NOT NULL DEFAULT '[]',
    target_tiers_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_arr_search_targets_time
ON arr_search_targets(app, event_time);

CREATE TABLE IF NOT EXISTS arr_profile_snapshot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    app TEXT NOT NULL,
    configured INTEGER NOT NULL DEFAULT 0,
    profiles INTEGER NOT NULL DEFAULT 0,
    entities INTEGER NOT NULL DEFAULT 0,
    targets_applied INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS collector_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS collector_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    version TEXT NOT NULL,
    script_path TEXT,
    db_path TEXT
);

CREATE TABLE IF NOT EXISTS arr_log_family_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    app TEXT NOT NULL,
    root_path TEXT NOT NULL,
    segments_found INTEGER NOT NULL DEFAULT 0,
    segments_read INTEGER NOT NULL DEFAULT 0,
    new_segments INTEGER NOT NULL DEFAULT 0,
    lines_read INTEGER NOT NULL DEFAULT 0,
    relevant_events INTEGER NOT NULL DEFAULT 0,
    live_events INTEGER NOT NULL DEFAULT 0,
    backfill_events INTEGER NOT NULL DEFAULT 0,
    reset_detected INTEGER NOT NULL DEFAULT 0
);

"""



def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    ensure_column(conn, "collector_runs", "source_errors_json", "TEXT")

    # Safe migrations for databases created by v7.0.
    ensure_column(conn, "prowlarr_stats_snapshots", "failed_queries", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "prowlarr_stats_snapshots", "failed_grabs", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "prowlarr_stats_snapshots", "failed_rss", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "sab_attempt_diagnostics", "failure_code", "TEXT")
    ensure_column(conn, "sab_attempt_diagnostics", "failure_confidence", "TEXT")
    for col in (
        "cancelled", "live_completed", "backfill_completed", "live_failed", "backfill_failed",
        "live_cancelled", "backfill_cancelled", "live_lifecycle_events", "backfill_lifecycle_events",
    ):
        ensure_column(conn, "sab_log_family_runs", col, "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "sab_log_family_runs", "continuity_status", "TEXT NOT NULL DEFAULT 'unknown'")
    ensure_column(conn, "sab_log_family_runs", "continuity_message", "TEXT")
    ensure_column(conn, "sab_log_family_runs", "continuity_overlap", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "sab_log_family_runs", "oldest_first_time", "TEXT")
    ensure_column(conn, "sab_log_family_runs", "newest_first_time", "TEXT")
    for col in ("live_searches", "backfill_searches", "live_diagnostics", "backfill_diagnostics"):
        ensure_column(conn, "prowlarr_log_family_runs", col, "INTEGER NOT NULL DEFAULT 0")
    for col in ("live_events", "backfill_events"):
        ensure_column(conn, "arr_log_family_runs", col, "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "arr_events", "search_mode", "TEXT")
    ensure_column(conn, "arr_events", "origin_confidence", "TEXT")
    ensure_column(conn, "arr_search_candidates", "quality_name", "TEXT")
    ensure_column(conn, "arr_search_candidates", "parsed_media_title", "TEXT")
    ensure_column(conn, "arr_search_candidates", "parsed_season", "INTEGER")
    ensure_column(conn, "arr_search_candidates", "parsed_episode", "INTEGER")
    ensure_column(conn, "arr_search_candidates", "media_match", "INTEGER")
    ensure_column(conn, "prowlarr_stats_snapshots", "avg_grab_response_ms", "REAL NOT NULL DEFAULT 0")
    ensure_column(conn, "prowlarr_log_searches", "query_signature", "TEXT")
    ensure_column(conn, "prowlarr_log_searches", "result_count", "INTEGER")
    ensure_column(conn, "provider_snapshots", "provider_label", "TEXT")
    ensure_column(conn, "provider_snapshots", "priority", "INTEGER")
    ensure_column(conn, "provider_snapshots", "connections", "INTEGER")
    ensure_column(conn, "provider_snapshots", "enabled", "INTEGER")
    return conn


def save_provider_snapshots(conn, collected_at: str, providers: list[ProviderStats]) -> None:
    conn.executemany(
        """
        INSERT INTO provider_snapshots(
            collected_at, provider, provider_label, articles_tried, articles_success,
            day_bytes, week_bytes, month_bytes, total_bytes, priority, connections, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                collected_at, p.name, p.label, p.tried, p.success, p.day_bytes,
                p.week_bytes, p.month_bytes, p.total_bytes, p.priority,
                p.connections, None if p.enabled is None else int(p.enabled)
            )
            for p in providers
        ],
    )


def save_prowlarr_indexers(conn, indexers: dict[int, dict[str, Any]]) -> None:
    ts = now_iso()
    for idx, item in indexers.items():
        conn.execute(
            """
            INSERT INTO prowlarr_indexers(
                indexer_id, name, protocol, enabled, priority, privacy, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(indexer_id) DO UPDATE SET
                name=excluded.name,
                protocol=excluded.protocol,
                enabled=excluded.enabled,
                priority=excluded.priority,
                privacy=excluded.privacy,
                updated_at=excluded.updated_at
            """,
            (
                idx,
                str(item.get("name", f"Indexer {idx}")),
                str(item.get("protocol", "")),
                int(bool(item.get("enable", item.get("enabled", False)))),
                numeric_total(item.get("priority")),
                str(item.get("privacy", "")),
                ts,
            ),
        )


def save_prowlarr_stats(
    conn,
    collected_at: str,
    indexers: dict[int, dict[str, Any]],
    stats_payload: Any,
) -> list[dict[str, Any]]:
    rows = extract_stats_rows(stats_payload)
    saved = []

    for row in rows:
        try:
            idx = int(row.get("indexerId", row.get("id")))
        except (TypeError, ValueError):
            continue
        if idx not in indexers:
            continue

        name = str(row.get("indexerName") or row.get("name") or indexers[idx].get("name") or idx)
        queries = stat_int(row, "numberOfQueries", "queries", "queryCount")
        grabs = stat_int(row, "numberOfGrabs", "grabs", "grabCount")
        failed_queries = stat_int(row, "numberOfFailedQueries", "failedQueries")
        failed_grabs = stat_int(row, "numberOfFailedGrabs", "failedGrabs")
        failed_rss = stat_int(row, "numberOfFailedRssQueries", "failedRssQueries")
        failures = failed_queries + failed_grabs + failed_rss
        avg_ms = stat_float(row, "averageResponseTime", "averageResponseTimeMs", "avgResponseTime")
        avg_grab_ms = stat_float(row, "averageGrabResponseTime", "averageGrabResponseTimeMs")

        conn.execute(
            """
            INSERT INTO prowlarr_stats_snapshots(
                collected_at,indexer_id,indexer_name,queries,grabs,failures,
                avg_response_ms,failed_queries,failed_grabs,failed_rss,avg_grab_response_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collected_at, idx, name, queries, grabs, failures, avg_ms,
                failed_queries, failed_grabs, failed_rss, avg_grab_ms
            ),
        )

        saved.append({
            "indexer_id": idx, "name": name, "queries": queries, "grabs": grabs,
            "failures": failures, "failed_queries": failed_queries,
            "failed_grabs": failed_grabs, "failed_rss": failed_rss,
            "avg_ms": avg_ms, "avg_grab_ms": avg_grab_ms,
        })
    return saved


def import_prowlarr_history(
    conn,
    indexers: dict[int, dict[str, Any]],
    records: list[dict[str, Any]],
) -> tuple[int, Counter]:
    inserted = 0
    types = Counter()

    for rec in records:
        idx = history_indexer_id(rec)
        if idx is None or idx not in indexers:
            continue  # Usenet only

        event_type = history_event_type(rec)
        types[event_type] += 1
        event_time = history_date(rec)
        title = history_title(rec)
        name = str(indexers[idx].get("name", idx))

        # Prefer Prowlarr's own history id if present.
        source_id = rec.get("id")
        event_id = (
            f"prowlarr:{source_id}"
            if source_id is not None
            else stable_hash(event_time, idx, event_type, title, json.dumps(rec, sort_keys=True))
        )

        cur = conn.execute(
            """
            INSERT INTO prowlarr_history(
                event_id, event_time, indexer_id, indexer_name,
                event_type, release_title, data_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                event_time=excluded.event_time,
                indexer_id=excluded.indexer_id,
                indexer_name=excluded.indexer_name,
                event_type=excluded.event_type,
                release_title=CASE
                    WHEN COALESCE(excluded.release_title, '') <> ''
                    THEN excluded.release_title
                    ELSE prowlarr_history.release_title
                END,
                data_json=COALESCE(excluded.data_json, prowlarr_history.data_json)
            """,
            (
                event_id, event_time, idx, name, event_type, title,
                json.dumps(rec.get("data"), ensure_ascii=False, sort_keys=True)
                if rec.get("data") is not None else None,
                now_iso(),
            ),
        )
        # sqlite rowcount is 1 for both insert and update here; count rows
        # that were processed/backfilled rather than calling them "new".
        if cur.rowcount > 0:
            inserted += 1

    return inserted, types


# ===========================================================================
# SAB rotating-log family parser (v7.7.1-compatible)
# ===========================================================================

SAB_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)::")
SAB_COMPLETED_RE = re.compile(
    r"Sending notification:\s+Download Completed\s+-\s+(?P<job>.+?)\s+\(type=complete\b",
    re.I,
)
SAB_FAILED_RE = re.compile(
    r"Sending notification:\s+Download Failed\s+-\s+(?P<job>.+?)\s+\(type=failed\b",
    re.I,
)
SAB_CANCELLED_RE = re.compile(
    r"Sending notification:\s+Download (?:Cancelled|Canceled|Aborted)\s+-\s+(?P<job>.+?)\s+\(type=",
    re.I,
)
SAB_QUEUE_END_RE = re.compile(r"Ending job\s+(?P<job>.+)$", re.I)
SAB_QUEUE_REMOVE_RE = re.compile(r"Removing job\s+(?P<job>.+)$", re.I)
SAB_PURGE_JOB_RE = re.compile(r"Purging data for job\s+(?P<job>.+?)\s+\(delete_all_data=(?P<delete>True|False)\)", re.I)
SAB_HISTORY_ARCHIVE_RE = re.compile(r"Moved job\s+(?P<nzo>[0-9a-f-]{20,})\s+to archive", re.I)
SAB_HISTORY_REMOVE_RE = re.compile(r"Removing job\s+(?P<nzo>[0-9a-f-]{20,})\s+from history", re.I)
SAB_MISSING_RE = re.compile(r"Article\s+.+?\s+unavailable on all servers,\s+discarding", re.I)
SAB_REPAIR_BLOCKS_RE = re.compile(
    r"(?:Prospectively\s+)?added\s+(?P<blocks>\d+)\s+(?:repair\s+)?blocks\s+to\s+(?P<job>.+)$", re.I
)
SAB_OTHER_REPAIR_RE = re.compile(
    r"(?:Repairing|Repair complete|Repairing completed|Quick-check will repair|Repair is required)",
    re.I,
)
SAB_NZB_ADD_RE = re.compile(r"Attempting to add\s+(?P<job>.+?)\.nzb\s*$", re.I)
SAB_POSTPROC_RE = re.compile(r"Starting Post-Processing on\s+(?P<job>.+?)\s+=>", re.I)
SAB_VERIFY_START_RE = re.compile(r"Starting verification and repair of\s+(?P<job>.+)$", re.I)
SAB_NO_PAR2_RE = re.compile(r"No par2 sets for\s+(?P<job>.+)$", re.I)
SAB_VERIFY_FINISHED_RE = re.compile(r"Verification and repair finished for\s+(?P<job>.+)$", re.I)
SAB_RUN_UNPACKER_RE = re.compile(r"Running unpacker on\s+(?P<job>.+)$", re.I)
SAB_FINISH_UNPACK_RE = re.compile(r"Finished unpack_magic on\s+(?P<job>.+)$", re.I)
SAB_UNRAR_START_RE = re.compile(r"Unrar starting on\s+(?P<path>.+)$", re.I)
SAB_UNRAR_FINISH_RE = re.compile(r"Unrar finished on\s+(?P<path>.+)$", re.I)
SAB_CRC_FAIL_RE = re.compile(
    r"RAR-based verification failed:\s*(?P<reason>.+?CRC check failed:\s*(?P<file>.+?)\s+-\s+exp=(?P<expected>\d+)\s+got=(?P<actual>\d+).*)$",
    re.I,
)
SAB_REPAIR_SHORT_RE = re.compile(r"Repair failed, not enough repair blocks\s*\((?P<short>\d+) short\)", re.I)
SAB_NEED_BLOCKS_RE = re.compile(r"Need\s+(?P<need>\d+)\s+more blocks", re.I)
SAB_BLOCKS_AVAILABLE_RE = re.compile(r"(?P<available>\d+)\s+blocks available", re.I)
SAB_PAR_FAIL_RE = re.compile(r"Par verify failed on\s+(?P<path>.+?)(?:!|$)", re.I)
SAB_PAR_OK_RE = re.compile(r"Par verify finished ok on\s+(?P<path>.+?)(?:!|$)", re.I)
SAB_REPAIR_REQUIRED_RE = re.compile(r"Verified in\s+(?P<seconds>\d+)\s+sec(?:ond)?s?, repair is required", re.I)
SAB_REPAIRED_RE = re.compile(r"Repaired in\s+(?P<seconds>\d+)\s+sec(?:ond)?s?", re.I)
SAB_DIRECT_ERROR_RE = re.compile(r"Error in DirectUnpack of\s+(?P<set>[^:]+):\s*(?P<reason>.+)$", re.I)
SAB_DIRECT_ABORT_RE = re.compile(r"Aborting DirectUnpack for\s+(?P<set>\S+)", re.I)
SAB_DIRECT_COMPLETE_RE = re.compile(r"DirectUnpack completed for\s+(?P<set>\S+)", re.I)
SAB_RAR_VERIFIED_RE = re.compile(r"RAR files verified successfully", re.I)
SAB_PASSWORD_FAIL_RE = re.compile(r"(?:password.*(?:failed|incorrect)|no password found)", re.I)


def _sab_family_sort_key(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    if name == "sabnzbd.log":
        return (10_000, name)
    m = re.match(r"sabnzbd\.log\.(\d+)(?:\.txt)?$", name)
    if m:
        # Largest suffix is oldest. Sort oldest -> newest.
        return (10_000 - int(m.group(1)), name)
    return (9_999, name)


def discover_sab_logs(source: Path) -> list[Path]:
    """Discover SAB's active log plus numbered rotated siblings.

    Accepts either the log directory or any one member of the family.  The
    returned list is chronological (oldest rotated segment first, active log
    last).  A non-standard single file is still accepted for compatibility.
    """
    source = source.expanduser()
    directory = source if source.is_dir() else source.parent
    if not directory.is_dir():
        return []

    family = []
    for p in directory.iterdir():
        if not p.is_file():
            continue
        low = p.name.lower()
        if low == "sabnzbd.log" or re.fullmatch(r"sabnzbd\.log\.\d+(?:\.txt)?", low):
            family.append(p)

    if family:
        # suffix .3 is older than .2; explicit key below produces .3,.2,.1,active
        def order(p: Path):
            low = p.name.lower()
            if low == "sabnzbd.log":
                return (1, 0)
            m = re.fullmatch(r"sabnzbd\.log\.(\d+)(?:\.txt)?", low)
            return (0, -int(m.group(1))) if m else (0, 0)
        return sorted(family, key=order)

    return [source] if source.is_file() else []


def _first_line_info(path: Path) -> tuple[str | None, str, bytes]:
    with path.open("rb") as f:
        first = f.readline()
    text = first.decode("utf-8", errors="replace").rstrip("\r\n")
    m = SAB_TS_RE.match(text)
    ts = parse_timestamp_any(m.group("ts")) if m else None
    line_hash = hashlib.sha256(first).hexdigest()
    return ts, line_hash, first


def sab_segment_id(path: Path) -> tuple[str, str | None, str]:
    first_ts, line_hash, _ = _first_line_info(path)
    # Filename and size are deliberately excluded: SAB renames .log -> .log.1
    # during rotation and the active file grows between collector runs.
    return stable_hash("sab-log-segment", first_ts or "", line_hash), first_ts, line_hash


def get_segment_offset(conn: sqlite3.Connection, segment_id: str, file_size: int) -> tuple[int, bool, bool]:
    row = conn.execute(
        "SELECT byte_offset,file_size FROM sab_log_segments WHERE segment_id=?", (segment_id,)
    ).fetchone()
    if row is None:
        return 0, False, True
    offset = int(row["byte_offset"] or 0)
    old_size = int(row["file_size"] or 0)
    if file_size < offset or file_size < old_size:
        return 0, True, False
    return offset, False, False


def set_segment_state(
    conn: sqlite3.Connection, segment_id: str, first_time: str | None,
    first_line_hash: str, path: Path, offset: int, file_size: int,
) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO sab_log_segments(
            segment_id,first_time,first_line_hash,current_path,byte_offset,file_size,
            first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(segment_id) DO UPDATE SET
            current_path=excluded.current_path,
            byte_offset=excluded.byte_offset,
            file_size=excluded.file_size,
            last_seen_at=excluded.last_seen_at
        """,
        (segment_id, first_time, first_line_hash, str(path.resolve()), offset, file_size, now, now),
    )


def _job_from_sab_path(value: str) -> str | None:
    text = str(value or "").replace("/", "\\")
    m = re.search(r"\\incomplete\\(?P<job>[^\\]+)(?:\\|$)", text, re.I)
    if m:
        return m.group("job").strip()
    m = re.search(r"\\complete\\[^\\]+\\_UNPACK_(?P<job>[^\\]+)(?:\\|$)", text, re.I)
    if m:
        return m.group("job").strip()
    return None


def _job_from_crc_filename(filename: str) -> str:
    name = str(filename or "").strip().strip('"\'')
    # SAB's CRC line normally names the resulting media file after the job.
    for ext in (".mkv", ".mp4", ".avi", ".m4v", ".ts"):
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return name


def insert_sab_log_event(
    conn: sqlite3.Connection, ts: str, job: str | None, stage: str,
    status: str, kind: str, message: str = "", details: dict[str, Any] | None = None,
    segment_id: str | None = None, raw_line: str = "",
) -> bool:
    job = (job or "").strip() or None
    message = " ".join(str(message or "").split())
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    event_id = stable_hash("sab-log-event", ts, job or "", stage, status, kind, message, details_json)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO sab_log_events(
            event_id,event_time,job_name,stage,status,kind,message,details_json,
            segment_id,raw_line,imported_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_id, ts, job, stage, status, kind, message, details_json,
         segment_id, raw_line[:4000], now_iso()),
    )
    return cur.rowcount > 0


def get_offset(conn, table: str, log_path: str, file_size: int) -> tuple[int, bool]:
    """Return the saved byte offset for a single incrementally-read log.

    SAB rotating logs use the segment-aware state helpers, while the
    Prowlarr log still uses the legacy ``log_state`` table.  Keep this helper
    for that single-file incremental reader.
    """
    row = conn.execute(
        "SELECT byte_offset, file_size FROM log_state WHERE log_path=?",
        (log_path,),
    ).fetchone()
    if row is None:
        return 0, False

    old_offset = int(row["byte_offset"])
    old_size = int(row["file_size"])
    if file_size < old_offset or file_size < old_size:
        return 0, True
    return old_offset, False


def set_offset(conn, log_path: str, offset: int, size: int) -> None:
    """Persist the byte offset for a single incrementally-read log."""
    conn.execute(
        """
        INSERT INTO log_state(log_path, byte_offset, file_size, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(log_path) DO UPDATE SET
            byte_offset=excluded.byte_offset,
            file_size=excluded.file_size,
            updated_at=excluded.updated_at
        """,
        (log_path, offset, size, now_iso()),
    )


def upsert_repair_state(conn, job: str, blocks: int, ts: str) -> None:
    row = conn.execute(
        "SELECT max_repair_blocks FROM repair_state WHERE job_name=?", (job,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO repair_state(job_name,max_repair_blocks,first_seen,last_seen) VALUES(?,?,?,?)",
            (job, blocks, ts, ts),
        )
    else:
        conn.execute(
            "UPDATE repair_state SET max_repair_blocks=?, last_seen=? WHERE job_name=?",
            (max(int(row["max_repair_blocks"]), blocks), ts, job),
        )
    # Repair evidence can arrive after the terminal SAB job row was inserted.
    # Keep the materialized job metric synchronized so Release Journey and all
    # downstream repair-rate calculations reflect late-observed PAR blocks.
    conn.execute(
        """UPDATE jobs
           SET max_repair_blocks = CASE
               WHEN COALESCE(max_repair_blocks, 0) < ? THEN ?
               ELSE COALESCE(max_repair_blocks, 0)
           END
           WHERE job_name=?""",
        (blocks, blocks, job),
    )


def repair_blocks_for_job(conn, job: str) -> int:
    row = conn.execute(
        "SELECT max_repair_blocks FROM repair_state WHERE job_name=?", (job,)
    ).fetchone()
    return int(row["max_repair_blocks"]) if row else 0


def insert_sab_job(conn, ts: str, job: str, status: str) -> bool:
    eid = stable_hash(ts, status, job)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO jobs(event_id,event_time,job_name,status,max_repair_blocks,imported_at)
        VALUES(?,?,?,?,?,?)
        """,
        (eid, ts, job, status, repair_blocks_for_job(conn, job), now_iso()),
    )
    return cur.rowcount > 0


def upsert_sab_cancelled_job(conn: sqlite3.Connection, ts: str, job: str) -> str:
    """Return inserted/reclassified/existing for a high-confidence cancellation."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        lo = (dt - timedelta(minutes=5)).isoformat(timespec="milliseconds")
        hi = (dt + timedelta(minutes=5)).isoformat(timespec="milliseconds")
        nearby = conn.execute(
            """SELECT event_id,status,event_time,job_name FROM jobs
               WHERE lower(status) IN ('failed','cancelled','canceled','aborted')
                 AND event_time BETWEEN ? AND ?
               ORDER BY ABS(julianday(event_time)-julianday(?))""",
            (lo, hi, ts),
        ).fetchall()
        target_key = normalized_match_title(job)
        same_job = [r for r in nearby if normalized_match_title(r["job_name"]) == target_key]
        if any(str(r["status"] or "").lower() in {'cancelled','canceled','aborted'} for r in same_job):
            return "existing"
        failed = next((r for r in same_job if str(r["status"] or "").lower() == 'failed'), None)
        if failed is not None:
            conn.execute("UPDATE jobs SET status='cancelled' WHERE event_id=?", (failed["event_id"],))
            return "reclassified"
    except (TypeError, ValueError, sqlite3.Error):
        pass
    return "inserted" if insert_sab_job(conn, ts, job, "cancelled") else "existing"


def insert_sab_cancelled_job(conn: sqlite3.Connection, ts: str, job: str) -> bool:
    """Persist a cancellation; True means a terminal row was inserted/reclassified."""
    return upsert_sab_cancelled_job(conn, ts, job) in {"inserted", "reclassified"}


def _sab_history_job_by_nzo(conn: sqlite3.Connection, nzo_id: str) -> str | None:
    """Resolve SAB's history UUID to a release name when the API has ever exposed it."""
    row = conn.execute(
        "SELECT job_name FROM sab_history WHERE nzo_id=? ORDER BY updated_at DESC LIMIT 1",
        (str(nzo_id or "").strip(),),
    ).fetchone()
    return str(row["job_name"]).strip() if row and row["job_name"] else None


def _parse_sab_segment(
    conn: sqlite3.Connection, path: Path, context: dict[str, Any]
) -> dict[str, Any]:
    stat = path.stat()
    segment_id, first_time, first_line_hash = sab_segment_id(path)
    offset, reset, is_new = get_segment_offset(conn, segment_id, stat.st_size)
    counts: dict[str, Any] = {
        "lines": 0, "completed": 0, "failed": 0, "cancelled": 0, "unavailable": 0,
        "repair": 0, "repair_jobs": 0, "events": 0, "reset": reset,
        "new_segment": is_new, "segment_id": segment_id, "path": str(path),
        "start_offset": offset, "end_offset": offset,
    }
    repair_jobs: set[str] = set()
    end = offset

    set_to_job: dict[str, str] = context.setdefault("set_to_job", {})
    pending_repair: dict[str, Any] = context.setdefault("pending_repair", {})
    recent_end: dict[str, datetime] = context.setdefault("recent_end", {})
    recent_remove: dict[str, dict[str, Any]] = context.setdefault("recent_remove", {})

    with path.open("rb") as f:
        f.seek(offset)
        for raw in complete_lines(f):
            counts["lines"] += 1
            end = f.tell()
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            m = SAB_TS_RE.match(line)
            ts = parse_timestamp_any(m.group("ts")) if m else now_iso()
            ts = ts or now_iso()

            if SAB_MISSING_RE.search(line):
                counts["unavailable"] += 1

            # SAB uses "Removing job" both for the normal queue -> post-processing
            # transition and for a user/system queue removal.  The surrounding
            # sequence distinguishes them: normal completion has "Ending job" and
            # immediately starts post-processing; a cancellation removes + purges
            # the job before any post-processing hand-off.
            current_dt = None
            try:
                current_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if current_dt.tzinfo is None:
                    current_dt = current_dt.astimezone()
            except (TypeError, ValueError):
                pass

            em = SAB_QUEUE_END_RE.search(line)
            if em and current_dt is not None:
                ended_job = em.group("job").strip()
                recent_end[normalized_match_title(ended_job)] = current_dt

            rm = SAB_QUEUE_REMOVE_RE.search(line)
            # Do not confuse the database-history "Removing job <uuid> from history"
            # message with a queue removal.
            if rm and " from history" not in line.lower():
                job = rm.group("job").strip()
                cancel_key = normalized_match_title(job)
                ended = False
                if current_dt is not None and cancel_key in recent_end:
                    ended = timedelta(0) <= current_dt - recent_end[cancel_key] <= timedelta(seconds=15)
                recent_remove[cancel_key] = {"time": current_dt, "transition": False, "ended": ended, "job": job}

            hm = SAB_HISTORY_ARCHIVE_RE.search(line)
            if hm:
                nzo = hm.group("nzo").strip()
                job = _sab_history_job_by_nzo(conn, nzo)
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "History", "info", "history_archived",
                    "SAB history item moved to archive", {"nzo_id": nzo}, segment_id, line,
                ))

            hm = SAB_HISTORY_REMOVE_RE.search(line)
            if hm:
                nzo = hm.group("nzo").strip()
                job = _sab_history_job_by_nzo(conn, nzo)
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "History", "info", "history_removed",
                    "SAB history item removed", {"nzo_id": nzo}, segment_id, line,
                ))

            # Learn set-id -> job mappings from any path carrying both.
            path_job = _job_from_sab_path(line)
            if path_job:
                mset = re.search(r"\\([^\\]+?)(?:\.part\d+\.rar|\.vol\d+[+-]\d+\.par2|\.par2)(?:['\"!]|\\|\s|$)", line, re.I)
                if mset:
                    set_to_job[mset.group(1)] = path_job

            pm = SAB_PURGE_JOB_RE.search(line)
            if pm:
                job = pm.group("job").strip()
                delete_all = str(pm.group("delete") or "").lower() == "true"
                removal = recent_remove.get(normalized_match_title(job)) or {}
                rdt = removal.get("time")
                near_remove = False
                if current_dt is not None and isinstance(rdt, datetime):
                    near_remove = timedelta(0) <= current_dt - rdt <= timedelta(seconds=15)
                # delete_all_data=False is used by ordinary failed/post-processing
                # transitions and is not cancellation evidence. Manual SAB removals
                # observed in both Sonarr and Radarr tests use delete_all_data=True.
                if delete_all and near_remove and not removal.get("transition") and not removal.get("ended"):
                    if insert_sab_cancelled_job(conn, ts, job):
                        counts["cancelled"] += 1
                    counts["events"] += int(insert_sab_log_event(
                        conn, ts, job, "Final", "warn", "download_cancelled",
                        "Removed from SAB queue before completion",
                        {"evidence": "queue_remove_then_purge_true", "confidence": "high",
                         "inferred_user_or_system_action": True},
                        segment_id, line,
                    ))
                    # Keep the candidate around for a few more lines so a DirectUnpack
                    # abort can still be associated with the same job, but prevent a
                    # second purge from creating another terminal state.
                    removal["cancel_recorded"] = True
                    removal["purge_true"] = True

            m = SAB_NZB_ADD_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Source", "ok", "nzb_added",
                    "NZB accepted by SABnzbd", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_REPAIR_BLOCKS_RE.search(line)
            if m:
                job = m.group("job").strip()
                blocks = int(m.group("blocks"))
                upsert_repair_state(conn, job, blocks, ts)
                repair_jobs.add(job)
                counts["repair"] += 1
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Download", "warn", "repair_blocks_observed",
                    f"SAB added {blocks} repair blocks",
                    {"blocks": blocks}, segment_id, line,
                ))
            elif SAB_OTHER_REPAIR_RE.search(line):
                counts["repair"] += 1

            m = SAB_POSTPROC_RE.search(line)
            if m:
                job = m.group("job").strip()
                cancel_key = normalized_match_title(job)
                if cancel_key in recent_remove:
                    recent_remove[cancel_key]["transition"] = True
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Post-processing", "started", "postproc_started",
                    "Post-processing started", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_VERIFY_START_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Verification", "started", "verification_started",
                    "Verification and repair started", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_NO_PAR2_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Repair", "warn", "no_par2",
                    "No PAR2 recovery set is available", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_CRC_FAIL_RE.search(line)
            if m:
                job = _job_from_crc_filename(m.group("file"))
                reason = f"CRC verification failed for {m.group('file')}: expected {m.group('expected')}, got {m.group('actual')}"
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Verification", "failed", "crc_verification_failed",
                    reason,
                    {"file": m.group("file"), "expected_crc": m.group("expected"), "actual_crc": m.group("actual")},
                    segment_id, line,
                ))

            m = SAB_REPAIR_REQUIRED_RE.search(line)
            if m:
                pending_repair["required"] = {"time": ts, "seconds": int(m.group("seconds"))}

            m = SAB_NEED_BLOCKS_RE.search(line)
            if m:
                pending_repair["needed"] = int(m.group("need"))

            m = SAB_BLOCKS_AVAILABLE_RE.search(line)
            if m:
                pending_repair["available"] = int(m.group("available"))

            m = SAB_REPAIR_SHORT_RE.search(line)
            if m:
                pending_repair["short"] = int(m.group("short"))
                pending_repair["failure_time"] = ts

            m = SAB_REPAIRED_RE.search(line)
            if m:
                pending_repair["repaired_seconds"] = int(m.group("seconds"))
                pending_repair["repaired_time"] = ts

            m = SAB_PAR_FAIL_RE.search(line)
            if m:
                job = _job_from_sab_path(m.group("path"))
                short = int(pending_repair.get("short") or 0)
                need = int(pending_repair.get("needed") or 0)
                available = int(pending_repair.get("available") or 0)
                if short:
                    reason = f"Repair failed: {short} repair blocks short"
                    kind = "repair_insufficient_blocks"
                else:
                    reason = "PAR verification failed"
                    kind = "par_verify_failed"
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Repair", "failed", kind, reason,
                    {"blocks_short": short, "blocks_needed": need, "blocks_available": available},
                    segment_id, line,
                ))
                pending_repair.clear()

            m = SAB_PAR_OK_RE.search(line)
            if m:
                job = _job_from_sab_path(m.group("path"))
                seconds = int(pending_repair.get("repaired_seconds") or 0)
                message = f"PAR verification/repair completed successfully"
                if seconds:
                    message += f" in {seconds}s"
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Repair", "ok", "par_verify_ok", message,
                    {"repair_seconds": seconds}, segment_id, line,
                ))
                pending_repair.clear()

            m = SAB_VERIFY_FINISHED_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Verification", "info", "verification_finished",
                    "Verification/repair phase finished", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_RUN_UNPACKER_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "started", "unpack_started",
                    "Unpack started", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_UNRAR_START_RE.search(line)
            if m:
                job = _job_from_sab_path(m.group("path"))
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "started", "unrar_started",
                    "UnRAR started", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_DIRECT_ERROR_RE.search(line)
            if m:
                set_id = m.group("set").strip()
                job = _job_from_sab_path(m.group("reason")) or set_to_job.get(set_id)
                if job:
                    set_to_job[set_id] = job
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "warn", "direct_unpack_error",
                    "DirectUnpack error: " + " ".join(m.group("reason").split()),
                    {"set_id": set_id}, segment_id, line,
                ))

            m = SAB_DIRECT_ABORT_RE.search(line)
            if m:
                set_id = m.group("set").strip()
                job = set_to_job.get(set_id)
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "warn", "direct_unpack_aborted",
                    "DirectUnpack aborted", {"set_id": set_id}, segment_id, line,
                ))

            m = SAB_DIRECT_COMPLETE_RE.search(line)
            if m:
                set_id = m.group("set").strip()
                job = set_to_job.get(set_id)
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "ok", "direct_unpack_completed",
                    "DirectUnpack completed", {"set_id": set_id}, segment_id, line,
                ))

            m = SAB_UNRAR_FINISH_RE.search(line)
            if m:
                job = _job_from_sab_path(m.group("path"))
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "ok", "unrar_finished",
                    "UnRAR finished", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_FINISH_UNPACK_RE.search(line)
            if m:
                job = m.group("job").strip()
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Unpack", "ok", "unpack_finished",
                    "Unpack completed", segment_id=segment_id, raw_line=line,
                ))

            if SAB_PASSWORD_FAIL_RE.search(line) and path_job:
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, path_job, "Unpack", "failed", "password_failure",
                    "Archive password failure", segment_id=segment_id, raw_line=line,
                ))

            m = SAB_COMPLETED_RE.search(line)
            if m:
                job = m.group("job").strip()
                if insert_sab_job(conn, ts, job, "completed"):
                    counts["completed"] += 1
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Final", "ok", "download_completed",
                    "Download completed", segment_id=segment_id, raw_line=line,
                ))
                continue

            m = SAB_CANCELLED_RE.search(line)
            if m:
                job = m.group("job").strip()
                if insert_sab_cancelled_job(conn, ts, job):
                    counts["cancelled"] += 1
                counts["events"] += int(insert_sab_log_event(
                    conn, ts, job, "Final", "warn", "download_cancelled",
                    "Download cancelled/aborted", segment_id=segment_id, raw_line=line,
                ))
                continue

            m = SAB_FAILED_RE.search(line)
            if m:
                job = m.group("job").strip()
                removal = recent_remove.get(normalized_match_title(job)) or {}
                rdt = removal.get("time")
                cancelled_recently = bool(removal.get("cancel_recorded"))
                if cancelled_recently and current_dt is not None and isinstance(rdt, datetime):
                    cancelled_recently = timedelta(0) <= current_dt - rdt <= timedelta(minutes=5)
                if cancelled_recently:
                    # SAB may emit a generic final failure after a queue purge.
                    # Preserve it as evidence, but do not turn a user/system
                    # cancellation into an indexer-quality failure.
                    insert_sab_cancelled_job(conn, ts, job)
                    counts["events"] += int(insert_sab_log_event(
                        conn, ts, job, "Final", "info", "download_failed_after_cancel",
                        "SAB emitted Download Failed after cancellation",
                        {"evidence": "recent_queue_cancel"}, segment_id, line,
                    ))
                else:
                    if insert_sab_job(conn, ts, job, "failed"):
                        counts["failed"] += 1
                    counts["events"] += int(insert_sab_log_event(
                        conn, ts, job, "Final", "failed", "download_failed",
                        "Download failed", segment_id=segment_id, raw_line=line,
                    ))

    counts["repair_jobs"] = len(repair_jobs)
    counts["end_offset"] = end
    set_segment_state(conn, segment_id, first_time, first_line_hash, path, end, stat.st_size)
    conn.execute(
        """
        INSERT INTO log_runs(
          collected_at,log_path,start_offset,end_offset,lines_read,
          unavailable_events,completed,failed,repair_events,jobs_with_repair_activity,reset_detected
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(), str(path.resolve()), offset, end, counts["lines"], counts["unavailable"],
            counts["completed"], counts["failed"], counts["repair"], counts["repair_jobs"], int(reset)
        ),
    )
    return counts


def _latest_collection_time(conn: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> str | None:
    sql = f"SELECT collected_at FROM {table}"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return str(row["collected_at"]) if row and row["collected_at"] else None


def _split_new_rows_by_cutoff(
    conn: sqlite3.Connection, table: str, event_time_col: str, run_started: str,
    previous_collected_at: str | None, where: str = "", params: tuple[Any, ...] = (),
) -> tuple[int, int]:
    """Return (live, backfill) for rows inserted during this parser invocation.

    On the first run there is no prior collection watermark, so all retained rows
    are deliberately labeled backfill. On later runs, event timestamps newer than
    the previous collector watermark are live; older newly-discovered rows are
    historical backfill.
    """
    sql = f"SELECT {event_time_col} AS event_time FROM {table} WHERE imported_at>=?"
    qparams: list[Any] = [run_started]
    if where:
        sql += " AND " + where
        qparams.extend(params)
    rows = conn.execute(sql, tuple(qparams)).fetchall()
    if previous_collected_at is None:
        return 0, len(rows)
    cutoff = parse_timestamp_any(previous_collected_at)
    if not cutoff:
        return 0, len(rows)
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.astimezone()
    live = 0
    backfill = 0
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row["event_time"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
        except (TypeError, ValueError):
            backfill += 1
            continue
        if dt > cutoff_dt:
            live += 1
        else:
            backfill += 1
    return live, backfill


def reconcile_retained_sab_cancellations(conn: sqlite3.Connection, source: Path) -> dict[str, int]:
    """Scan retained SAB logs every run for high-confidence queue cancellations.

    SAB has three visually similar removal flows. Normal queue -> post-processing has
    ``Ending job`` followed by ``Removing job`` and ``Starting Post-Processing``.
    History cleanup says ``Removing job <uuid> from history``. A manual/user/system
    queue cancellation observed in both Sonarr and Radarr tests uses ``Removing job``
    followed immediately by ``Purging data ... delete_all_data=True`` and often
    ``Aborting DirectUnpack``. This pass is intentionally independent of incremental
    byte offsets so a cancellation cannot be lost just because an older collector
    advanced the segment watermark before cancellation support was added.
    """
    state_name = "sab_cancel_reconcile_v785"
    stats = {
        "jobs": 0, "events": 0, "lines_scanned": 0,
        "remove_candidates": 0, "purge_true_candidates": 0,
        "direct_unpack_abort": 0, "accepted": 0,
        "inserted": 0, "reclassified": 0, "already_recorded": 0,
        "rejected_ending": 0, "rejected_postproc": 0,
        "rejected_no_purge": 0,
    }
    paths = discover_sab_logs(source)
    if not paths:
        return stats

    recent_end: dict[str, datetime] = {}
    candidates: dict[str, dict[str, Any]] = {}
    set_to_job: dict[str, str] = {}

    def finalize(key: str, candidate: dict[str, Any]) -> None:
        if candidate.get("finalized"):
            return
        candidate["finalized"] = True
        if candidate.get("ended"):
            stats["rejected_ending"] += 1
            return
        if candidate.get("postproc"):
            stats["rejected_postproc"] += 1
            return
        if not candidate.get("purge_true"):
            stats["rejected_no_purge"] += 1
            return
        stats["accepted"] += 1
        action = upsert_sab_cancelled_job(conn, candidate["time_iso"], candidate["job"])
        if action == "inserted":
            stats["inserted"] += 1
            stats["jobs"] += 1
        elif action == "reclassified":
            stats["reclassified"] += 1
            stats["jobs"] += 1
        else:
            stats["already_recorded"] += 1

        cdt = candidate["time"]
        lo = (cdt - timedelta(seconds=10)).isoformat(timespec="milliseconds")
        hi = (cdt + timedelta(seconds=20)).isoformat(timespec="milliseconds")
        existing = conn.execute(
            """SELECT 1 FROM sab_log_events
               WHERE job_name=? AND kind='download_cancelled'
                 AND event_time BETWEEN ? AND ? LIMIT 1""",
            (candidate["job"], lo, hi),
        ).fetchone()
        if existing is None:
            evidence = "queue_remove_purge_true_directunpack_abort" if candidate.get("abort") else "queue_remove_then_purge_true"
            if insert_sab_log_event(
                conn, candidate["time_iso"], candidate["job"], "Final", "warn", "download_cancelled",
                "Removed from SAB queue before completion",
                {
                    "evidence": evidence,
                    "confidence": "high",
                    "inferred_user_or_system_action": True,
                    "reconciliation": state_name,
                },
                None, candidate.get("purge_line") or candidate.get("remove_line") or "",
            ):
                stats["events"] += 1

    def prune(now_dt: datetime) -> None:
        for key, candidate in list(candidates.items()):
            cdt = candidate.get("time")
            if isinstance(cdt, datetime) and now_dt - cdt > timedelta(seconds=45):
                finalize(key, candidate)
                candidates.pop(key, None)

    for path in paths:
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for raw in fh:
                stats["lines_scanned"] += 1
                line = raw.rstrip("\r\n")
                tm = SAB_TS_RE.match(line)
                ts = parse_timestamp_any(tm.group("ts")) if tm else None
                if not ts:
                    continue
                try:
                    current_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if current_dt.tzinfo is None:
                        current_dt = current_dt.astimezone()
                except (TypeError, ValueError):
                    continue
                prune(current_dt)

                path_job = _job_from_sab_path(line)
                if path_job:
                    sm = re.search(r"\\([^\\]+?)(?:\.part\d+\.rar|\.vol\d+[+-]\d+\.par2|\.par2)(?:['\"!]|\\|\s|$)", line, re.I)
                    if sm:
                        set_to_job[sm.group(1)] = path_job

                em = SAB_QUEUE_END_RE.search(line)
                if em:
                    ended_job = em.group("job").strip()
                    recent_end[normalized_match_title(ended_job)] = current_dt

                rm = SAB_QUEUE_REMOVE_RE.search(line)
                if rm and " from history" not in line.lower():
                    job = rm.group("job").strip()
                    key = normalized_match_title(job)
                    if key in candidates:
                        finalize(key, candidates[key])
                    ended = False
                    if key in recent_end:
                        delta = current_dt - recent_end[key]
                        ended = timedelta(0) <= delta <= timedelta(seconds=15)
                    candidates[key] = {
                        "job": job, "time": current_dt, "time_iso": ts,
                        "ended": ended, "postproc": False, "purge_true": False,
                        "abort": False, "remove_line": line,
                    }
                    stats["remove_candidates"] += 1
                    continue

                post = SAB_POSTPROC_RE.search(line)
                if post:
                    key = normalized_match_title(post.group("job").strip())
                    candidate = candidates.get(key)
                    if candidate and timedelta(0) <= current_dt - candidate["time"] <= timedelta(seconds=30):
                        candidate["postproc"] = True
                        finalize(key, candidate)
                        candidates.pop(key, None)
                    continue

                pm = SAB_PURGE_JOB_RE.search(line)
                if pm:
                    job = pm.group("job").strip()
                    key = normalized_match_title(job)
                    candidate = candidates.get(key)
                    if candidate and timedelta(0) <= current_dt - candidate["time"] <= timedelta(seconds=15):
                        if str(pm.group("delete") or "").lower() == "true":
                            candidate["purge_true"] = True
                            candidate["purge_line"] = line
                            stats["purge_true_candidates"] += 1
                    continue

                am = SAB_DIRECT_ABORT_RE.search(line)
                if am:
                    set_id = am.group("set").strip()
                    mapped = set_to_job.get(set_id)
                    matched = None
                    if mapped:
                        matched = candidates.get(normalized_match_title(mapped))
                    if matched is None:
                        open_candidates = [
                            c for c in candidates.values()
                            if timedelta(0) <= current_dt - c["time"] <= timedelta(seconds=10)
                        ]
                        if len(open_candidates) == 1:
                            matched = open_candidates[0]
                    if matched is not None and not matched.get("abort"):
                        matched["abort"] = True
                        stats["direct_unpack_abort"] += 1
                    continue

                # Explicit terminal markers close the candidate promptly. A generic
                # Download Failed after a remove+purge(true) is cancellation fallout;
                # successful completion or a normal post-processing hand-off rejects it.
                fm = SAB_COMPLETED_RE.search(line)
                if fm:
                    key = normalized_match_title(fm.group("job").strip())
                    candidate = candidates.get(key)
                    if candidate:
                        candidate["postproc"] = True
                        finalize(key, candidate)
                        candidates.pop(key, None)
                    continue
                ff = SAB_FAILED_RE.search(line)
                if ff:
                    key = normalized_match_title(ff.group("job").strip())
                    candidate = candidates.get(key)
                    if candidate:
                        finalize(key, candidate)
                        candidates.pop(key, None)

    for key, candidate in list(candidates.items()):
        finalize(key, candidate)

    # collector_migrations doubles as a durable diagnostics/state store here. Unlike
    # older one-shot migrations this row is replaced every run so Diagnostics always
    # reports what the most recent retained-log reconciliation actually observed.
    conn.execute(
        """INSERT OR REPLACE INTO collector_migrations(name,applied_at,details)
           VALUES(?,?,?)""",
        (state_name, now_iso(), json.dumps(stats, sort_keys=True)),
    )
    return stats


def backfill_retained_sab_cancellations(conn: sqlite3.Connection, source: Path) -> dict[str, int]:
    """Backward-compatible alias for the v7.8.6 retained-log reconciliation."""
    return reconcile_retained_sab_cancellations(conn, source)


def reconcile_sab_history_cancellations(conn: sqlite3.Connection) -> dict[str, int]:
    """Reclassify failed job rows when SAB History explicitly records cancellation.

    Queue deletion is best detected from the rotating log remove/purge sequence, but
    post-processing cancellation can survive only in SAB History where SAB reports a
    failed status with text such as ``Post-processing was aborted``.  Treat only
    explicit user/system abort wording as cancellation; generic failure/repair text is
    intentionally left untouched.
    """
    rows = conn.execute(
        """SELECT history_id,completed_time,job_name,status,fail_message,stage_log_json
           FROM sab_history
           WHERE lower(COALESCE(status,''))='failed' AND COALESCE(completed_time,'')<>''
           ORDER BY completed_time"""
    ).fetchall()
    changed = events = candidates = 0
    explicit = re.compile(
        r"(?i)\b(post-processing was aborted|post processing was aborted|download (?:was )?(?:cancelled|canceled|aborted)|"
        r"job (?:was )?(?:cancelled|canceled|aborted)|(?:cancelled|canceled|aborted) by (?:user|system))\b"
    )
    for h in rows:
        texts = [str(h["fail_message"] or "")]
        try:
            stages = json.loads(h["stage_log_json"] or "[]")
        except Exception:
            stages = []
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                actions = stage.get("actions") or []
                if not isinstance(actions, list):
                    actions = [actions]
                texts.extend(str(a or "") for a in actions)
        evidence = " | ".join(" ".join(t.split()) for t in texts if t)
        if not explicit.search(evidence):
            continue
        candidates += 1
        try:
            dt = datetime.fromisoformat(str(h["completed_time"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
        except (TypeError, ValueError):
            continue
        lo = (dt - timedelta(minutes=10)).isoformat(timespec="milliseconds")
        hi = (dt + timedelta(minutes=10)).isoformat(timespec="milliseconds")
        job = str(h["job_name"] or "").strip()
        failed = conn.execute(
            """SELECT event_id,event_time,job_name FROM jobs
               WHERE lower(status)='failed' AND event_time BETWEEN ? AND ?
               ORDER BY ABS(julianday(event_time)-julianday(?))""",
            (lo, hi, h["completed_time"]),
        ).fetchall()
        target = next(
            (r for r in failed if normalized_match_title(r["job_name"]) == normalized_match_title(job)),
            None,
        )
        if target is None:
            continue
        conn.execute("UPDATE jobs SET status='cancelled' WHERE event_id=?", (target["event_id"],))
        changed += 1
        if insert_sab_log_event(
            conn, str(h["completed_time"]), job, "Final", "warn", "download_cancelled",
            "SAB History explicitly records cancellation/abort",
            {"evidence": "sab_history", "history_id": h["history_id"], "history_text": evidence[:1000]},
            None, evidence[:4000],
        ):
            events += 1
    return {"candidates": candidates, "jobs": changed, "events": events}


def parse_sab_log_family(conn: sqlite3.Connection, source: Path) -> dict[str, Any]:
    paths = discover_sab_logs(source)
    if not paths:
        raise FileNotFoundError(f"No SAB log files found under: {source}")

    run_started = now_iso()
    previous_collected_at = _latest_collection_time(conn, "sab_log_family_runs")

    # SAB only retains a small rotating family.  Content IDs let us tell whether at
    # least one file from the previous observation is still present.  If the entire
    # family has changed between collector runs, we cannot prove loss, but there is
    # no continuity anchor and Diagnostics should explicitly warn about a possible
    # evidence gap instead of silently claiming complete coverage.
    known_segment_ids = {str(r[0]) for r in conn.execute("SELECT segment_id FROM sab_log_segments")}
    current_segment_info = [sab_segment_id(path) for path in paths]
    current_segment_ids = {str(info[0]) for info in current_segment_info}
    continuity_overlap = len(known_segment_ids & current_segment_ids)
    first_times = [str(info[1]) for info in current_segment_info if info[1]]
    oldest_first_time = min(first_times) if first_times else None
    newest_first_time = max(first_times) if first_times else None
    continuity_status = "baseline"
    continuity_message = "Initial SAB rotation baseline captured; continuity can be checked on the next run."
    if previous_collected_at is not None and known_segment_ids:
        if continuity_overlap > 0:
            continuity_status = "ok"
            continuity_message = f"Rotation continuity anchored by {continuity_overlap} previously observed retained segment(s)."
        else:
            continuity_status = "unverified"
            continuity_message = "No previously observed SAB segment remains in the current rotating family."
            try:
                prev_dt = datetime.fromisoformat(str(previous_collected_at).replace("Z", "+00:00"))
                old_dt = datetime.fromisoformat(str(oldest_first_time).replace("Z", "+00:00")) if oldest_first_time else None
                if prev_dt.tzinfo is None:
                    prev_dt = prev_dt.astimezone()
                if old_dt is not None and old_dt.tzinfo is None:
                    old_dt = old_dt.astimezone()
                if old_dt is not None and old_dt > prev_dt + timedelta(minutes=2):
                    continuity_status = "possible_gap"
                    continuity_message = (
                        "Possible SAB evidence gap: all currently retained log segments are new and "
                        "the oldest surviving segment starts after the previous collector run."
                    )
            except (TypeError, ValueError):
                pass

    totals: dict[str, Any] = {
        "lines": 0, "completed": 0, "failed": 0, "cancelled": 0, "unavailable": 0,
        "repair": 0, "repair_jobs": 0, "events": 0, "reset": False,
        "segments_found": len(paths), "segments_read": 0, "new_segments": 0,
        "paths": [str(p) for p in paths],
        "continuity_status": continuity_status, "continuity_message": continuity_message,
        "continuity_overlap": continuity_overlap, "oldest_first_time": oldest_first_time,
        "newest_first_time": newest_first_time,
    }
    all_repair_jobs = set()
    context: dict[str, Any] = {}
    for path in paths:
        info = _parse_sab_segment(conn, path, context)
        if info["lines"] > 0:
            totals["segments_read"] += 1
        totals["new_segments"] += int(bool(info["new_segment"]))
        totals["reset"] = bool(totals["reset"] or info["reset"])
        for key in ("lines", "completed", "failed", "cancelled", "unavailable", "repair", "events"):
            totals[key] += int(info[key])
    # Distinct jobs with observed repair blocks in the lines read this run.
    # Use the persisted event table because a job can span segment boundaries.
    totals["repair_jobs"] = int(conn.execute(
        """SELECT COUNT(DISTINCT job_name) FROM sab_log_events
           WHERE kind='repair_blocks_observed' AND COALESCE(job_name,'')<>''"""
    ).fetchone()[0])
    live_completed, backfill_completed = _split_new_rows_by_cutoff(
        conn, "jobs", "event_time", run_started, previous_collected_at, "status='completed'"
    )
    live_failed, backfill_failed = _split_new_rows_by_cutoff(
        conn, "jobs", "event_time", run_started, previous_collected_at, "status='failed'"
    )
    live_cancelled, backfill_cancelled = _split_new_rows_by_cutoff(
        conn, "jobs", "event_time", run_started, previous_collected_at, "status='cancelled'"
    )
    live_lifecycle, backfill_lifecycle = _split_new_rows_by_cutoff(
        conn, "sab_log_events", "event_time", run_started, previous_collected_at
    )
    totals.update({
        "live_completed": live_completed, "backfill_completed": backfill_completed,
        "live_failed": live_failed, "backfill_failed": backfill_failed,
        "live_cancelled": live_cancelled, "backfill_cancelled": backfill_cancelled,
        "live_lifecycle_events": live_lifecycle, "backfill_lifecycle_events": backfill_lifecycle,
    })
    conn.execute(
        """
        INSERT INTO sab_log_family_runs(
            collected_at,root_path,segments_found,segments_read,new_segments,lines_read,
            unavailable_events,completed,failed,cancelled,repair_events,lifecycle_events,
            live_completed,backfill_completed,live_failed,backfill_failed,live_cancelled,backfill_cancelled,
            live_lifecycle_events,backfill_lifecycle_events,reset_detected,
            continuity_status,continuity_message,continuity_overlap,oldest_first_time,newest_first_time
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(), str(source.resolve()), int(totals["segments_found"]),
            int(totals["segments_read"]), int(totals["new_segments"]), int(totals["lines"]),
            int(totals["unavailable"]), int(totals["completed"]), int(totals["failed"]), int(totals["cancelled"]),
            int(totals["repair"]), int(totals["events"]),
            int(live_completed), int(backfill_completed), int(live_failed), int(backfill_failed),
            int(live_cancelled), int(backfill_cancelled),
            int(live_lifecycle), int(backfill_lifecycle), int(bool(totals["reset"])),
            continuity_status, continuity_message, int(continuity_overlap), oldest_first_time, newest_first_time,
        ),
    )
    return totals


def _history_failure_stage(message: str) -> str | None:
    msg = str(message or "").lower()
    if not msg:
        return None
    if any(k in msg for k in ("repair", "par2", "par verify", "repair blocks", "blocks short")):
        return "Repair"
    if any(k in msg for k in ("unpack", "unrar", "7zip", "password", "rar")):
        return "Unpack"
    if any(k in msg for k in ("crc", "verify", "verification", "corrupt")):
        return "Verification"
    if any(k in msg for k in ("article", "download", "missing", "cannot be completed", "not complete")):
        return "Download"
    if "script" in msg:
        return "Script"
    return "Post-processing"


def _classify_failure_detail(kind: str | None, message: str | None, stage: str | None) -> tuple[str, str]:
    k = str(kind or "").lower()
    msg = str(message or "").lower()
    st = str(stage or "").lower()
    if k == "repair_insufficient_blocks" or "blocks short" in msg or "not enough repair" in msg:
        return "repair_insufficient", "high"
    if k == "crc_verification_failed" or "crc" in msg:
        return "verification_crc", "high"
    if k == "par_verify_failed":
        return "repair_failed", "high"
    if k == "password_failure" or "password" in msg or "encrypted" in msg:
        return "password_encrypted", "high"
    if k in {"direct_unpack_error", "direct_unpack_aborted"} or "unpack" in st or "unrar" in msg or "7zip" in msg:
        return "unpack", "high"
    if any(x in msg for x in ("missing article", "articles missing", "not enough articles", "cannot be completed", "not complete")):
        return "missing_articles", "medium"
    if any(x in msg for x in ("disk full", "no space left", "permission denied", "i/o error", "input/output error", "file write")):
        return "disk_file_io", "medium"
    if any(x in msg for x in ("cancelled", "canceled", "aborted")):
        return "cancelled_aborted", "medium"
    if any(x in msg for x in ("download", "article")):
        return "download", "medium"
    if k == "no_par2":
        return "repair_unavailable", "medium"
    return "cause_not_retained", "none"


def rebuild_sab_attempt_diagnostics(conn: sqlite3.Connection) -> int:
    """Reconstruct per-attempt lifecycle/failure cause from log + History API.

    The final jobs table remains the attempt ledger.  Log evidence is the
    authoritative timeline where available; History API failure/stage data is
    used as structured enrichment/fallback.
    """
    jobs = conn.execute(
        "SELECT event_id,event_time,job_name,status FROM jobs ORDER BY event_time"
    ).fetchall()
    events = conn.execute(
        """SELECT event_time,job_name,stage,status,kind,message,details_json
           FROM sab_log_events WHERE COALESCE(job_name,'')<>'' ORDER BY event_time"""
    ).fetchall()
    history = conn.execute(
        """SELECT history_id,completed_time,job_name,status,fail_message,stage_log_json
           FROM sab_history ORDER BY completed_time"""
    ).fetchall()

    event_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    event_groups_loose: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for e in events:
        event_groups[normalized_release_identity(e["job_name"])].append(e)
        event_groups_loose[normalized_match_title(e["job_name"])].append(e)

    hist_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    hist_groups_loose: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for h in history:
        hist_groups[normalized_release_identity(h["job_name"])].append(h)
        hist_groups_loose[normalized_match_title(h["job_name"])].append(h)

    previous_final: dict[str, datetime] = {}
    updated = 0
    failure_priority = {
        "repair_insufficient_blocks": 100,
        "crc_verification_failed": 95,
        "par_verify_failed": 90,
        "password_failure": 85,
        "direct_unpack_error": 70,
        "no_par2": 30,
        "download_cancelled": 110,
        "download_failed": 10,
    }
    stage_order = ["Source", "Download", "Verification", "Repair", "Unpack", "Post-processing", "Final"]

    for j in jobs:
        final_dt = datetime.fromisoformat(str(j["event_time"]).replace("Z", "+00:00"))
        if final_dt.tzinfo is None:
            final_dt = final_dt.astimezone()
        ident = normalized_release_identity(j["job_name"])
        loose = normalized_match_title(j["job_name"])
        prev = previous_final.get(ident)
        window_start = max(final_dt - timedelta(hours=6), prev + timedelta(milliseconds=1) if prev else final_dt - timedelta(hours=6))
        previous_final[ident] = final_dt

        candidates = event_groups.get(ident) or event_groups_loose.get(loose, [])
        selected = []
        for e in candidates:
            edt = datetime.fromisoformat(str(e["event_time"]).replace("Z", "+00:00"))
            if edt.tzinfo is None:
                edt = edt.astimezone()
            in_attempt = window_start <= edt <= final_dt + timedelta(seconds=10)
            post_terminal_history = (
                str(e["kind"] or "").lower() in {"history_archived", "history_removed"}
                and final_dt <= edt <= final_dt + timedelta(hours=2)
            )
            if in_attempt or post_terminal_history:
                selected.append(e)

        # Nearest matching SAB history row within one hour, preferring same status.
        hbest = None
        hscore = None
        hcands = hist_groups.get(ident) or hist_groups_loose.get(loose, [])
        for h in hcands:
            if not h["completed_time"]:
                continue
            try:
                hdt = datetime.fromisoformat(str(h["completed_time"]).replace("Z", "+00:00"))
                if hdt.tzinfo is None:
                    hdt = hdt.astimezone()
            except ValueError:
                continue
            distance = abs((hdt - final_dt).total_seconds())
            if distance > 3600:
                continue
            penalty = 0 if str(h["status"] or "").lower() == str(j["status"]).lower() else 600
            score = distance + penalty
            if hscore is None or score < hscore:
                hbest, hscore = h, score

        lifecycle_map: dict[str, dict[str, Any]] = {}
        failure_event = None
        failure_score = -1
        for e in selected:
            stage = str(e["stage"] or "Other")
            bucket = lifecycle_map.setdefault(stage, {"name": stage, "status": "info", "events": []})
            st = str(e["status"] or "info")
            if str(j["status"]).lower() == "completed" and str(e["kind"] or "") == "no_par2":
                # Absence of PAR2 is informational when the job still completed.
                st = "info"
            rank = {"info": 0, "started": 0, "ok": 1, "warn": 2, "failed": 3}.get(st, 0)
            old_rank = {"info": 0, "started": 0, "ok": 1, "warn": 2, "failed": 3}.get(bucket["status"], 0)
            if rank > old_rank:
                bucket["status"] = st
            try:
                details = json.loads(e["details_json"] or "{}")
            except Exception:
                details = {}
            bucket["events"].append({
                "time": e["event_time"], "kind": e["kind"], "status": st,
                "message": e["message"] or "", "details": details,
            })
            score = failure_priority.get(str(e["kind"]), 0)
            if st in ("failed", "warn") and score > failure_score:
                failure_event, failure_score = e, score

        # History stage_log is useful when log evidence is absent or sparse.
        history_fail = ""
        history_stages = []
        if hbest is not None:
            history_fail = " ".join(str(hbest["fail_message"] or "").split())
            try:
                raw = json.loads(hbest["stage_log_json"] or "[]")
            except Exception:
                raw = []
            if isinstance(raw, list):
                for stage in raw:
                    if not isinstance(stage, dict):
                        continue
                    name = str(stage.get("name") or "Stage").strip()
                    actions = stage.get("actions") or []
                    if not isinstance(actions, list):
                        actions = [actions]
                    clean = [re.sub(r"<[^>]+>", "", str(a)).replace("<br>", " · ").strip() for a in actions]
                    clean = [" ".join(x.split()) for x in clean if x.strip()]
                    history_stages.append({"name": name, "actions": clean})
                    if name not in lifecycle_map and clean:
                        lifecycle_map[name] = {
                            "name": name, "status": "info",
                            "events": [{"time": hbest["completed_time"], "kind": "history_stage", "status": "info", "message": a, "details": {}} for a in clean],
                        }

        # A completed job can contain a transient PAR/verification failure followed
        # by an explicit successful retry in the same stage. Keep the failure line
        # as evidence, but present the stage as a warning/recovered condition rather
        # than a red terminal failure.
        if str(j["status"] or "").lower() == "completed":
            for stage_name in ("Verification", "Repair"):
                bucket = lifecycle_map.get(stage_name)
                if not bucket:
                    continue
                evs = bucket.get("events") or []
                recovery_indexes = [
                    i for i, ev in enumerate(evs)
                    if str(ev.get("status") or "") == "ok"
                    or str(ev.get("kind") or "") in {"par_verify_ok", "verification_finished"}
                ]
                if not recovery_indexes:
                    continue
                last_recovery = max(recovery_indexes)
                recovered_failure = False
                for i, ev in enumerate(evs):
                    if i < last_recovery and str(ev.get("status") or "") == "failed":
                        ev["status"] = "warn"
                        details = ev.get("details") if isinstance(ev.get("details"), dict) else {}
                        details["recovered_later"] = True
                        ev["details"] = details
                        recovered_failure = True
                if recovered_failure:
                    bucket["status"] = "warn"
                    bucket["recovered"] = True

        failure_stage = None
        failure_code = None
        failure_confidence = None
        failure_reason = None
        if str(j["status"]).lower() == "failed":
            if failure_event is not None and str(failure_event["kind"]) != "download_failed":
                failure_stage = str(failure_event["stage"] or "Post-processing")
                failure_reason = str(failure_event["message"] or "Download failed")
            elif history_fail:
                failure_stage = _history_failure_stage(history_fail)
                failure_reason = history_fail
            elif failure_event is not None and str(failure_event["kind"]) != "download_failed":
                failure_stage = str(failure_event["stage"] or "Post-processing")
                failure_reason = str(failure_event["message"] or "Download failed")
            else:
                failure_stage = "Cause unavailable"
                failure_reason = "SAB marked the attempt failed, but no specific causal message was retained in the parsed logs or History API."

            if failure_event is not None and str(failure_event["kind"] or "") != "download_failed":
                failure_code, failure_confidence = _classify_failure_detail(
                    failure_event["kind"], failure_reason, failure_stage
                )
            elif history_fail:
                failure_code, failure_confidence = _classify_failure_detail(
                    None, history_fail, failure_stage
                )
            else:
                failure_code, failure_confidence = "cause_not_retained", "none"
        elif str(j["status"]).lower() in {"cancelled", "canceled", "aborted"}:
            cancel_event = next((e for e in reversed(selected) if str(e["kind"] or "").lower() == "download_cancelled"), None)
            failure_stage = "Cancelled"
            failure_code = "cancelled_aborted"
            failure_confidence = "high" if cancel_event is not None else "medium"
            failure_reason = str(cancel_event["message"] if cancel_event is not None else "Download cancelled or aborted before completion")

        lifecycle = []
        for stage in stage_order:
            if stage in lifecycle_map:
                lifecycle.append(lifecycle_map.pop(stage))
        lifecycle.extend(lifecycle_map.values())

        source = "log+history" if selected and hbest is not None else "log" if selected else "history" if hbest is not None else "inferred"
        conn.execute(
            """
            INSERT INTO sab_attempt_diagnostics(
                event_id,job_name,final_time,final_status,failure_stage,failure_code,failure_confidence,failure_reason,
                lifecycle_json,source,history_id,log_event_count,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                failure_stage=excluded.failure_stage,
                failure_code=excluded.failure_code,
                failure_confidence=excluded.failure_confidence,
                failure_reason=excluded.failure_reason,
                lifecycle_json=excluded.lifecycle_json,
                source=excluded.source,
                history_id=excluded.history_id,
                log_event_count=excluded.log_event_count,
                updated_at=excluded.updated_at
            """,
            (
                j["event_id"], j["job_name"], j["event_time"], j["status"],
                failure_stage, failure_code, failure_confidence, failure_reason,
                json.dumps(lifecycle, ensure_ascii=False), source,
                hbest["history_id"] if hbest is not None else None, len(selected), now_iso(),
            ),
        )
        updated += 1
    return updated


def parse_sab_log(conn, path: Path) -> dict[str, Any]:
    """Backward-compatible alias; rotated siblings are auto-discovered."""
    return parse_sab_log_family(conn, path)


# ===========================================================================
# Prowlarr log incremental parser
# ===========================================================================

PROW_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\|")
PROW_SEARCH_RE = re.compile(
    r"ReleaseSearchService\|Searching indexer\(s\): \[(?P<indexers>[^\]]+)\](?P<rest>.*)$",
    re.I,
)
PROW_TERM_RE = re.compile(r"for Term: \[(?P<term>[^\]]*)\]", re.I)
PROW_RESULTS_RE = re.compile(
    r"ReleaseSearchService\|Total of (?P<count>\d+) reports were found(?P<rest>.*?) from 1 indexer\(s\)",
    re.I,
)

def _prowlarr_query_signature(rest: str) -> str:
    text = re.sub(r"\s+", " ", str(rest or "").strip())
    # Search lines carry the same criteria after the indexer name that the
    # corresponding per-indexer result line repeats.  Normalizing punctuation
    # and whitespace gives us a durable way to pair the asynchronous result
    # count back to the correct indexer without relying on line ordering.
    return re.sub(r"\s*([,:|])\s*", r"\1", text).lower()
PROW_CONNECT_RE = re.compile(
    r"Unable to connect to indexer \[(?P<url>https?://[^\]]+)\]", re.I
)
PROW_INVALID_RE = re.compile(
    r"Invalid Release: '.*?' from indexer: (?P<indexer>[^.]+)\.", re.I
)


def indexer_from_url(url: str, known_names: list[str]) -> str | None:
    low = url.lower()
    for n in known_names:
        token = re.sub(r"[^a-z0-9]+", "", n.lower())
        hostish = re.sub(r"[^a-z0-9]+", "", low)
        if token and token in hostish:
            return n
    return None


def parse_prowlarr_log(conn, path: Path, known_indexer_names: list[str]) -> dict[str, Any]:
    stat = path.stat()
    key = "prowlarr::" + str(path.resolve())
    offset, reset = get_offset(conn, "log_state", key, stat.st_size)
    end = offset
    counts = Counter()
    per_indexer_search = Counter()

    with path.open("rb") as f:
        f.seek(offset)
        for raw in complete_lines(f):
            counts["lines"] += 1
            end = f.tell()
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

            tsm = PROW_TS_RE.match(line)
            ts = parse_timestamp_any(tsm.group("ts")) if tsm else now_iso()
            ts = ts or now_iso()

            if "|Warn|" in line:
                counts["warnings"] += 1

            sm = PROW_SEARCH_RE.search(line)
            if sm:
                term_m = PROW_TERM_RE.search(sm.group("rest"))
                term = term_m.group("term") if term_m else ""
                for indexer in [x.strip() for x in sm.group("indexers").split(",")]:
                    if indexer not in known_indexer_names:
                        continue
                    eid = stable_hash(ts, "search", indexer, line)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO prowlarr_log_searches(
                            event_id,event_time,indexer_name,query_text,raw_line,imported_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (eid, ts, indexer, term, line, now_iso()),
                    )
                    per_indexer_search[indexer] += 1
                    counts["searches"] += 1
                continue

            cm = PROW_CONNECT_RE.search(line)
            if cm:
                idx_name = indexer_from_url(cm.group("url"), known_indexer_names)
                eid = stable_hash(ts, "connection_failure", idx_name or "", line)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO prowlarr_log_errors(
                        event_id,event_time,indexer_name,kind,message,imported_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (eid, ts, idx_name, "connection_failure", line, now_iso()),
                )
                counts["connection_failures"] += 1
                continue

            im = PROW_INVALID_RE.search(line)
            if im:
                idx_name = im.group("indexer").strip()
                if idx_name in known_indexer_names:
                    eid = stable_hash(ts, "invalid_release", idx_name, line)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO prowlarr_log_errors(
                            event_id,event_time,indexer_name,kind,message,imported_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (eid, ts, idx_name, "invalid_release", line, now_iso()),
                    )
                    counts["invalid_releases"] += 1

    set_offset(conn, key, end, stat.st_size)
    conn.execute(
        """
        INSERT INTO prowlarr_log_runs(
          collected_at,log_path,start_offset,end_offset,lines_read,searches,
          connection_failures,invalid_releases,warnings,reset_detected
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(), str(path.resolve()), offset, end, counts["lines"], counts["searches"],
            counts["connection_failures"], counts["invalid_releases"], counts["warnings"], int(reset)
        ),
    )
    return {
        "reset": reset,
        "counts": counts,
        "per_indexer_search": per_indexer_search,
    }



# ===========================================================================
# Rotating Prowlarr + Sonarr/Radarr log families (v7.7.1)
# ===========================================================================

ARR_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\|"
    r"(?P<level>[^|]+)\|(?P<component>[^|]+)\|(?P<message>.*)$"
)
ARR_SEARCH_RE = re.compile(
    r"Searching indexers for \[(?P<media>.+?)\]\.\s*(?P<count>\d+)\s+active indexers?",
    re.I,
)
ARR_SONARR_SENT_RE = re.compile(
    r"Report sent to SABnzbd\.\s*Indexer\s+(?P<indexer>.+?)\s+\(Prowlarr\)\.\s*(?P<release>.+)$",
    re.I,
)
ARR_RADARR_SENT_RE = re.compile(
    r"Report for\s+(?P<media>.+?)\s+sent to SABnzbd from indexer\s+"
    r"(?P<indexer>.+?)\s+\(Prowlarr\)\.\s*(?P<release>.+)$",
    re.I,
)
ARR_QUEUE_RE = re.compile(r"Adding report \[(?P<release>.+?)\] to the queue\.", re.I)
ARR_COULDNT_ADD_RE = re.compile(
    r"Couldn't add release '(?P<release>.+)' from Indexer\s+"
    r"(?P<indexer>.+?)\s+\(Prowlarr\)\s+to download queue\.",
    re.I,
)
ARR_API_GRAB_LIMIT_RE = re.compile(r"API Grab Limit reached for\s+(?P<url>\S+)", re.I)
ARR_MOVIE_ASSIGN_RE = re.compile(
    r"Assigning file \[(?P<file>.+?)\] to movie \[\[(?P<media>.+?)\]\[",
    re.I,
)
ARR_SEARCH_COMPLETE_RE = re.compile(
    r"(?:Episode|Season|Movie) search completed\..*?(?P<count>\d+)\s+reports? downloaded",
    re.I,
)
ARR_DECISION_PROCESS_RE = re.compile(r"Processing\s+(?P<count>\d+)\s+releases", re.I)
ARR_PROCESS_RELEASE_RE = re.compile(
    r"Processing release '(?P<release>.+?)' from '(?P<indexer>.+?)(?: \(Prowlarr\))?'$", re.I
)
ARR_QUALITY_PARSED_RE = re.compile(r"Quality parsed:\s*(?P<quality>.+?)(?:\s+v\d+)?$", re.I)
ARR_MOVIE_PARSED_RE = re.compile(r"Movie Parsed\.\s*(?P<title>.+?)\s+-\s+(?P<year>\d{4})\s*$", re.I)
ARR_EPISODE_PARSED_RE = re.compile(
    r"Episode Parsed\.\s*(?P<title>.+?)\s+-\s*S(?P<season>\d{1,2})(?:E(?P<episode>\d{1,3}))?\s*$", re.I
)
ARR_RELEASE_REJECTED_RE = re.compile(
    r"Release '(?P<release>.+?)' from '(?P<indexer>.+?)(?: \(Prowlarr\))?' rejected for the following reasons:\s*(?P<reasons>.+)$",
    re.I,
)
ARR_RELEASE_ACCEPTED_RE = re.compile(
    r"Release '(?P<release>.+?)' from '(?P<indexer>.+?)(?: \(Prowlarr\))?' accepted$",
    re.I,
)

def _quality_name(value: str | None, release: str | None = None) -> str | None:
    text = " ".join(str(value or "").split())
    text = re.sub(r"\s+v\d+$", "", text, flags=re.I).strip()
    if text:
        return text
    # Fallback is deliberately coarse and only used when the explicit parser line
    # was not retained. Exact target matching requires Arr's parsed quality name.
    rel = str(release or "")
    for pat, name in (
        (r"(?i)\b(?:remux)[ ._-]*2160p\b|\b2160p.*\bremux\b", "Remux-2160p"),
        (r"(?i)\b(?:remux)[ ._-]*1080p\b|\b1080p.*\bremux\b", "Remux-1080p"),
        (r"(?i)\b2160p\b.*\bblu[ ._-]?ray\b|\buhd[ ._-]?blu[ ._-]?ray\b", "Bluray-2160p"),
        (r"(?i)\b1080p\b.*\bblu[ ._-]?ray\b|\bblu[ ._-]?ray\b.*\b1080p\b", "Bluray-1080p"),
        (r"(?i)\b2160p\b.*\bweb[ ._-]?dl\b", "WEBDL-2160p"),
        (r"(?i)\b1080p\b.*\bweb[ ._-]?dl\b", "WEBDL-1080p"),
        (r"(?i)\b2160p\b.*\bwebrip\b", "WEBRip-2160p"),
        (r"(?i)\b1080p\b.*\bwebrip\b", "WEBRip-1080p"),
    ):
        if re.search(pat, rel):
            return name
    return None

def _quality_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

def _quality_tier(value: str | None, release: str | None = None) -> str | None:
    text = f"{value or ''} {release or ''}".lower()
    for token, label in (("2160", "2160p"), ("1080", "1080p"), ("720", "720p"), ("576", "SD"), ("480", "SD")):
        if token in text:
            return label
    if re.search(r"\b(?:sd|dvdrip|dvd)\b", text):
        return "SD"
    return None

def _nearest_arr_search(conn: sqlite3.Connection, app: str, ts: str, minutes: int = 20):
    current_dt = datetime.fromisoformat(ts)
    lower = (current_dt - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT event_id,event_time,media_title,season,episode,search_mode,origin_confidence
           FROM arr_events WHERE app=? AND event_type='search_started'
             AND event_time<=? AND event_time>=? ORDER BY event_time DESC LIMIT 1""",
        (app, ts, lower),
    ).fetchone()

def _upsert_search_candidate(
    conn: sqlite3.Connection, *, search_row: sqlite3.Row, ts: str, app: str,
    indexer: str, release: str, quality_raw: str | None = None,
    decision: str = "considered", rejection_reasons: str | None = None,
) -> str:
    indexer = re.sub(r"\s*\(Prowlarr\)\s*$", "", str(indexer or "").strip(), flags=re.I)
    candidate_id = stable_hash("arr-search-candidate", search_row["event_id"], indexer.lower(), normalized_match_title(release))
    quality_name = _quality_name(quality_raw, release)
    tier = _quality_tier(quality_name or quality_raw, release)
    conn.execute(
        """INSERT INTO arr_search_candidates(
               candidate_id,search_event_id,event_time,app,media_title,season,episode,indexer,release_name,
               quality_raw,quality_name,quality_tier,decision,rejection_reasons,imported_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(candidate_id) DO UPDATE SET
               quality_raw=COALESCE(excluded.quality_raw,arr_search_candidates.quality_raw),
               quality_name=COALESCE(excluded.quality_name,arr_search_candidates.quality_name),
               quality_tier=COALESCE(excluded.quality_tier,arr_search_candidates.quality_tier),
               decision=CASE
                   WHEN excluded.decision='selected' THEN 'selected'
                   WHEN excluded.decision='accepted' AND arr_search_candidates.decision<>'selected' THEN 'accepted'
                   WHEN excluded.decision='rejected' AND arr_search_candidates.decision NOT IN ('selected','accepted') THEN 'rejected'
                   ELSE arr_search_candidates.decision END,
               rejection_reasons=COALESCE(excluded.rejection_reasons,arr_search_candidates.rejection_reasons),
               updated_at=excluded.updated_at""",
        (candidate_id, search_row["event_id"], ts, app, search_row["media_title"], search_row["season"],
         search_row["episode"], indexer, release, quality_raw, quality_name, tier, decision, rejection_reasons, now_iso(), now_iso()),
    )
    return candidate_id
ARR_AUTO_SEARCH_BEGIN_RE = re.compile(r"Performing search for\s+\d+\s+(?:movies|episodes)", re.I)
ARR_RSS_START_RE = re.compile(r"Starting RSS Sync", re.I)
ARR_RSS_COMPLETE_RE = re.compile(r"RSS Sync Completed\.", re.I)
ARR_RSS_COMPLETE_DETAIL_RE = re.compile(
    r"RSS Sync Completed\.\s*Reports found:\s*(?P<found>\d+),\s*Reports grabbed:\s*(?P<grabbed>\d+)(?:,\s*Reports pending:\s*(?P<pending>\d+))?",
    re.I,
)
ARR_IMPORT_CANCEL_RE = re.compile(r"\b(?:cancelled|canceled|aborted)\b", re.I)
ARR_DOWNLOAD_CANCEL_RE = re.compile(
    r"\b(?:download|tracked download|queue item)\b.*\b(?:cancelled|canceled|removed|deleted)\b|"
    r"\b(?:cancelled|canceled|removed|deleted)\b.*\b(?:download|queue)\b",
    re.I,
)
ARR_SAB_UNREACHABLE_RE = re.compile(
    r"Unable to retrieve queue and history items from SABnzbd|Unable to connect to SABnzbd",
    re.I,
)
ARR_TRACKED_FAILED_RE = re.compile(
    r"Tracking\s+'SABnzbd:(?P<release>.+?)':\s*ClientState=Failed\b(?P<rest>.*)$",
    re.I,
)
ARR_REDOWNLOAD_RE = re.compile(
    r"Failed download .*?searching again\.?",
    re.I,
)
ARR_MARK_IMPORTED_RE = re.compile(
    r"\[(?P<release>.+?)\]\s+Marking download as imported from SABnzbd",
    re.I,
)
ARR_REMOVE_HISTORY_RE = re.compile(
    r"\[(?P<release>.+?)\]\s+Removing download from SABnzbd history",
    re.I,
)
ARR_QUEUE_DELETE_API_RE = re.compile(
    r"\[DELETE\]\s+/api/v3/queue(?:/bulk)?\?(?P<query>[^:]+):\s*200\.OK",
    re.I,
)
ARR_SONARR_MEDIA_RE = re.compile(
    r"^(?P<series>.+?)\s*:\s*S(?P<season>\d{1,2})(?:E(?P<episode>\d{1,3}))?\s*$",
    re.I,
)


def _numbered_text_log_order(path: Path, stem: str) -> tuple[int, int]:
    low = path.name.lower()
    active = f"{stem.lower()}.txt"
    if low == active:
        return (1, 0)
    m = re.fullmatch(re.escape(stem.lower()) + r"\.(\d+)\.txt", low)
    return (0, -int(m.group(1))) if m else (0, 0)


def discover_numbered_text_logs(source: Path, stem: str, include_debug: bool = False) -> list[Path]:
    """Return retained text-log families in stable rotation order.

    The normal family is ``stem.txt``, ``stem.0.txt``, ``stem.1.txt`` ... .
    When ``include_debug`` is true, the parallel ``stem.debug.txt`` and
    ``stem.debug.N.txt`` family is included too.  Normal logs are parsed first,
    then debug logs; row-level event ids deduplicate repeated Info lines while
    debug-only evidence remains available.
    """
    source = source.expanduser()
    directory = source if source.is_dir() else source.parent
    if not directory.is_dir():
        return []

    def one_family(family_stem: str) -> list[Path]:
        active = f"{family_stem.lower()}.txt"
        pattern = re.compile(re.escape(family_stem.lower()) + r"\.\d+\.txt$")
        rows = [
            p for p in directory.iterdir()
            if p.is_file() and (p.name.lower() == active or pattern.fullmatch(p.name.lower()))
        ]
        return sorted(rows, key=lambda p: _numbered_text_log_order(p, family_stem))

    family = one_family(stem)
    if include_debug:
        family.extend(one_family(f"{stem}.debug"))
    if family:
        # A path cannot normally belong to both families, but keep this defensive
        # de-duplication because source directories can contain links/copies.
        seen: set[str] = set()
        out: list[Path] = []
        for path in family:
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        return out
    return [source] if source.is_file() else []


def _first_timestamped_line(path: Path, ts_re: re.Pattern[str]) -> tuple[str | None, str]:
    first_raw = b""
    first_ts = None
    with path.open("rb") as f:
        for _ in range(128):
            raw = f.readline()
            if not raw:
                break
            if not first_raw:
                first_raw = raw
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            m = ts_re.match(line)
            if m:
                first_raw = raw
                first_ts = parse_timestamp_any(m.group("ts"))
                break
    return first_ts, hashlib.sha256(first_raw).hexdigest()


def _generic_segment_id(kind: str, path: Path, ts_re: re.Pattern[str]) -> tuple[str, str | None, str]:
    first_ts, line_hash = _first_timestamped_line(path, ts_re)
    return stable_hash(kind, first_ts or "", line_hash), first_ts, line_hash


def _get_simple_segment_offset(
    conn: sqlite3.Connection,
    table: str,
    segment_id: str,
    file_size: int,
) -> tuple[int, bool, bool]:
    row = conn.execute(
        f"SELECT byte_offset,file_size FROM {table} WHERE segment_id=?",
        (segment_id,),
    ).fetchone()
    if row is None:
        return 0, False, True
    offset = int(row["byte_offset"] or 0)
    old_size = int(row["file_size"] or 0)
    if file_size < offset or file_size < old_size:
        return 0, True, False
    return offset, False, False


def _set_simple_segment_state(
    conn: sqlite3.Connection,
    table: str,
    segment_id: str,
    first_time: str | None,
    line_hash: str,
    path: Path,
    offset: int,
    file_size: int,
) -> None:
    now = now_iso()
    conn.execute(
        f"""
        INSERT INTO {table}(
            segment_id,first_time,first_line_hash,current_path,byte_offset,file_size,
            first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(segment_id) DO UPDATE SET
            current_path=excluded.current_path,
            byte_offset=excluded.byte_offset,
            file_size=excluded.file_size,
            last_seen_at=excluded.last_seen_at
        """,
        (segment_id, first_time, line_hash, str(path.resolve()), offset, file_size, now, now),
    )


def _process_prowlarr_line(
    conn: sqlite3.Connection,
    line: str,
    known_indexer_names: list[str],
    counts: Counter,
    per_indexer_search: Counter,
) -> None:
    tsm = PROW_TS_RE.match(line)
    ts = parse_timestamp_any(tsm.group("ts")) if tsm else now_iso()
    ts = ts or now_iso()

    if "|Warn|" in line:
        counts["warnings"] += 1

    known_by_lower = {str(name).strip().lower(): str(name).strip() for name in known_indexer_names}

    sm = PROW_SEARCH_RE.search(line)
    if sm:
        term_m = PROW_TERM_RE.search(sm.group("rest"))
        term = term_m.group("term") if term_m else ""
        signature = _prowlarr_query_signature(sm.group("rest"))
        for raw_indexer in [x.strip() for x in sm.group("indexers").split(",")]:
            indexer = known_by_lower.get(raw_indexer.lower())
            if not indexer:
                continue
            eid = stable_hash(ts, "search", indexer, line)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO prowlarr_log_searches(
                    event_id,event_time,indexer_name,query_text,query_signature,result_count,raw_line,imported_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (eid, ts, indexer, term, signature, None, line, now_iso()),
            )
            if cur.rowcount:
                per_indexer_search[indexer] += 1
                counts["searches"] += 1
            else:
                conn.execute(
                    "UPDATE prowlarr_log_searches SET query_signature=COALESCE(query_signature, ?) WHERE event_id=?",
                    (signature, eid),
                )
        return

    rm = PROW_RESULTS_RE.search(line)
    if rm:
        signature = _prowlarr_query_signature(rm.group("rest"))
        result_count = int(rm.group("count"))
        # Result lines are asynchronous and omit the indexer name, but they repeat
        # the exact criteria from the initiating per-indexer search. Pair on that
        # signature inside a short time window.
        current_dt = datetime.fromisoformat(ts)
        lower = (current_dt - timedelta(minutes=2)).isoformat(timespec="seconds")
        row = conn.execute(
            """SELECT event_id FROM prowlarr_log_searches
               WHERE event_time<=? AND event_time>=? AND query_signature=?
               ORDER BY event_time DESC LIMIT 1""",
            (ts, lower, signature),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE prowlarr_log_searches SET result_count=? WHERE event_id=?",
                (result_count, row["event_id"]),
            )
        return

    cm = PROW_CONNECT_RE.search(line)
    if cm:
        idx_name = indexer_from_url(cm.group("url"), known_indexer_names)
        eid = stable_hash(ts, "connection_failure", idx_name or "", line)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO prowlarr_log_errors(
                event_id,event_time,indexer_name,kind,message,imported_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (eid, ts, idx_name, "connection_failure", line, now_iso()),
        )
        if cur.rowcount:
            counts["connection_failures"] += 1
        return

    im = PROW_INVALID_RE.search(line)
    if im:
        idx_name = known_by_lower.get(im.group("indexer").strip().lower())
        if idx_name:
            eid = stable_hash(ts, "invalid_release", idx_name, line)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO prowlarr_log_errors(
                    event_id,event_time,indexer_name,kind,message,imported_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (eid, ts, idx_name, "invalid_release", line, now_iso()),
            )
            if cur.rowcount:
                counts["invalid_releases"] += 1


def parse_prowlarr_log_family(
    conn: sqlite3.Connection,
    source: Path,
    known_indexer_names: list[str],
) -> dict[str, Any]:
    """Incrementally parse active + numbered Prowlarr rotations as one stream."""
    run_started = now_iso()
    previous_collected_at = _latest_collection_time(conn, "prowlarr_log_family_runs")
    family = discover_numbered_text_logs(source, "prowlarr", include_debug=True)
    counts = Counter()
    per_indexer_search = Counter()
    segments_read = 0
    new_segments = 0
    reset_detected = False

    for path in family:
        stat = path.stat()
        segment_kind = "prowlarr-debug-log-segment" if ".debug" in path.name.lower() else "prowlarr-log-segment"
        segment_id, first_ts, line_hash = _generic_segment_id(segment_kind, path, PROW_TS_RE)
        offset, reset, is_new = _get_simple_segment_offset(
            conn, "prowlarr_log_segments", segment_id, stat.st_size
        )
        if is_new:
            new_segments += 1
        reset_detected = reset_detected or reset

        end = offset
        before_lines = counts["lines"]
        with path.open("rb") as f:
            f.seek(offset)
            for raw in complete_lines(f):
                counts["lines"] += 1
                end = f.tell()
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                _process_prowlarr_line(conn, line, known_indexer_names, counts, per_indexer_search)

        if counts["lines"] > before_lines:
            segments_read += 1
        _set_simple_segment_state(
            conn, "prowlarr_log_segments", segment_id, first_ts, line_hash,
            path, end, stat.st_size,
        )

    live_searches, backfill_searches = _split_new_rows_by_cutoff(
        conn, "prowlarr_log_searches", "event_time", run_started, previous_collected_at
    )
    live_diag, backfill_diag = _split_new_rows_by_cutoff(
        conn, "prowlarr_log_errors", "event_time", run_started, previous_collected_at
    )
    counts["live_searches"] = live_searches
    counts["backfill_searches"] = backfill_searches
    counts["live_diagnostics"] = live_diag
    counts["backfill_diagnostics"] = backfill_diag
    collected = now_iso()
    root = str(source.resolve())
    conn.execute(
        """
        INSERT INTO prowlarr_log_family_runs(
            collected_at,root_path,segments_found,segments_read,new_segments,lines_read,
            searches,connection_failures,invalid_releases,warnings,
            live_searches,backfill_searches,live_diagnostics,backfill_diagnostics,reset_detected
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            collected, root, len(family), segments_read, new_segments, counts["lines"],
            counts["searches"], counts["connection_failures"], counts["invalid_releases"],
            counts["warnings"], live_searches, backfill_searches, live_diag, backfill_diag,
            int(reset_detected),
        ),
    )
    # Keep the legacy aggregate table populated so v2.8 and older views still work.
    conn.execute(
        """
        INSERT INTO prowlarr_log_runs(
          collected_at,log_path,start_offset,end_offset,lines_read,searches,
          connection_failures,invalid_releases,warnings,reset_detected
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            collected, root, 0, 0, counts["lines"], counts["searches"],
            counts["connection_failures"], counts["invalid_releases"],
            counts["warnings"], int(reset_detected),
        ),
    )
    return {
        "reset": reset_detected,
        "counts": counts,
        "per_indexer_search": per_indexer_search,
        "segments_found": len(family),
        "segments_read": segments_read,
        "new_segments": new_segments,
    }


def _arr_segment_offset(
    conn: sqlite3.Connection,
    app: str,
    segment_id: str,
    file_size: int,
) -> tuple[int, bool, bool]:
    row = conn.execute(
        """SELECT byte_offset,file_size FROM arr_log_segments
           WHERE app=? AND segment_id=?""",
        (app, segment_id),
    ).fetchone()
    if row is None:
        return 0, False, True
    offset = int(row["byte_offset"] or 0)
    old_size = int(row["file_size"] or 0)
    if file_size < offset or file_size < old_size:
        return 0, True, False
    return offset, False, False


def _set_arr_segment_state(
    conn: sqlite3.Connection,
    app: str,
    segment_id: str,
    first_time: str | None,
    line_hash: str,
    path: Path,
    offset: int,
    file_size: int,
) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO arr_log_segments(
            app,segment_id,first_time,first_line_hash,current_path,byte_offset,file_size,
            first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(app,segment_id) DO UPDATE SET
            current_path=excluded.current_path,
            byte_offset=excluded.byte_offset,
            file_size=excluded.file_size,
            last_seen_at=excluded.last_seen_at
        """,
        (
            app, segment_id, first_time, line_hash, str(path.resolve()), offset,
            file_size, now, now,
        ),
    )


def _arr_media_parts(app: str, raw: str) -> tuple[str, int | None, int | None]:
    raw = " ".join(str(raw or "").split())
    if app == "sonarr":
        m = ARR_SONARR_MEDIA_RE.match(raw)
        if m:
            return (
                m.group("series").strip(),
                int(m.group("season")),
                int(m.group("episode")) if m.group("episode") else None,
            )
    return raw, None, None


def _parsed_media_matches_search(
    app: str, search_title: str | None, search_season: int | None, search_episode: int | None,
    parsed_title: str | None, parsed_season: int | None = None, parsed_episode: int | None = None,
) -> bool:
    def clean(value: str | None) -> str:
        text = normalized_match_title(value or "")
        text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
        return " ".join(text.split())
    if not search_title or not parsed_title:
        return False
    search_clean = clean(search_title)
    parsed_clean = clean(parsed_title)
    if not search_clean or not parsed_clean:
        return False
    # Scene/alternate titles often add words (for example "Arcane" is parsed as
    # "Arcane League of Legends"). Treat exact and containment matches as the
    # same media, but still reject unrelated metadata false-positives such as
    # "Hill Street" being returned for "21 Jump Street".
    title_match = search_clean == parsed_clean
    if not title_match and min(len(search_clean), len(parsed_clean)) >= 4:
        title_match = search_clean in parsed_clean or parsed_clean in search_clean
    if not title_match:
        return False
    if str(app or "").lower() == "sonarr":
        if search_season is not None and parsed_season is not None and int(search_season) != int(parsed_season):
            return False
        if search_episode is not None:
            return parsed_episode is not None and int(search_episode) == int(parsed_episode)
    return True


def arr_media_matches_release(
    app: str, media_title: str | None, season: int | None, episode: int | None, release_title: str
) -> bool:
    """Collector-side conservative media/release matcher used for origin evidence."""
    if not media_title or not release_title:
        return False
    app = str(app or "").lower()
    if app == "sonarr":
        rep = episode_identity(release_title)
        if not rep:
            return False
        release_series, release_season, release_episode = rep
        media_series = normalized_match_title(media_title)
        media_series = re.sub(r"\b(?:19|20)\d{2}\b", " ", media_series)
        media_series = " ".join(media_series.split())
        if media_series != release_series:
            return False
        if season is not None and int(season) != release_season:
            return False
        if episode is not None and int(episode) != release_episode:
            return False
        return True
    media = normalized_match_title(media_title)
    release = normalized_match_title(release_title)
    return bool(media and len(media) >= 4 and media in release)


def _insert_arr_event(
    conn: sqlite3.Connection,
    *,
    ts: str,
    app: str,
    component: str,
    event_type: str,
    search_mode: str | None = None,
    origin_confidence: str | None = None,
    media_title: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    release_name: str | None = None,
    indexer: str | None = None,
    download_client: str | None = None,
    severity: str = "info",
    reason: str | None = None,
    message: str = "",
    details: dict[str, Any] | None = None,
    segment_id: str | None = None,
    raw_line: str = "",
) -> bool:
    clean_message = " ".join(str(message or "").split())
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)

    # Debug logs repeat many Info lines.  Reuse an already-retained core event
    # rather than manufacturing a duplicate simply because the debug pass can
    # now attach stronger origin evidence.
    existing = conn.execute(
        """SELECT event_id,search_mode,origin_confidence,details_json
           FROM arr_events
           WHERE app=? AND event_time=? AND COALESCE(component,'')=COALESCE(?, '')
             AND event_type=?
             AND COALESCE(media_title,'')=COALESCE(?, '')
             AND COALESCE(season,-1)=COALESCE(?, -1)
             AND COALESCE(episode,-1)=COALESCE(?, -1)
             AND COALESCE(release_name,'')=COALESCE(?, '')
             AND COALESCE(indexer,'')=COALESCE(?, '')
           LIMIT 1""",
        (app, ts, component, event_type, media_title, season, episode, release_name, indexer),
    ).fetchone()
    if existing is not None:
        rank = {"": 0, "none": 0, "low": 1, "medium": 2, "high": 3}
        old_conf = str(existing["origin_confidence"] or "none").lower()
        new_conf = str(origin_confidence or "none").lower()
        old_mode = str(existing["search_mode"] or "unknown").lower()
        new_mode = str(search_mode or "unknown").lower()
        if new_mode not in {"", "unknown"} and (
            old_mode in {"", "unknown"} or rank.get(new_conf, 0) > rank.get(old_conf, 0)
        ):
            conn.execute(
                "UPDATE arr_events SET search_mode=?,origin_confidence=? WHERE event_id=?",
                (new_mode, new_conf, existing["event_id"]),
            )
        # Keep richer debug details when the original Info row carried none.
        if details and str(existing["details_json"] or "{}") in {"", "{}"}:
            conn.execute(
                "UPDATE arr_events SET details_json=? WHERE event_id=?",
                (details_json, existing["event_id"]),
            )
        return False

    event_id = stable_hash(
        "arr-event", app, ts, component, event_type, search_mode or "", media_title or "",
        season if season is not None else "", episode if episode is not None else "",
        release_name or "", indexer or "", reason or "", clean_message,
    )
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO arr_events(
            event_id,event_time,app,component,event_type,search_mode,origin_confidence,media_title,season,episode,
            release_name,indexer,download_client,severity,reason,message,details_json,
            segment_id,raw_line,imported_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, ts, app, component, event_type, search_mode, origin_confidence, media_title, season, episode,
            release_name, indexer, download_client, severity, reason, clean_message,
            details_json, segment_id, raw_line[:4000], now_iso(),
        ),
    )
    return cur.rowcount > 0


def _parse_arr_line(
    conn: sqlite3.Connection,
    app: str,
    line: str,
    segment_id: str,
    recent_grab_errors: dict[str, tuple[datetime, str]],
    state: dict[str, Any],
) -> int:
    m = ARR_LINE_RE.match(line)
    if not m:
        return 0
    ts = parse_timestamp_any(m.group("ts")) or now_iso()
    level = m.group("level").strip().lower()
    component = m.group("component").strip()
    message = m.group("message").strip()
    severity = "error" if level == "error" else "warn" if level == "warn" else "info"
    inserted = 0
    low_component = component.lower()
    low_message = message.lower()
    current_dt = datetime.fromisoformat(ts)

    # High-value debug evidence.  These lines are intentionally narrow so enabling
    # Arr debug logging improves attribution without turning verbose implementation
    # noise into user-visible events.
    tf = ARR_TRACKED_FAILED_RE.search(message)
    if low_component == "trackeddownloadservice" and tf:
        release = tf.group("release").strip()
        state["last_failed_download"] = {"time": current_dt, "release": release}
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="download_failed_observed",
            release_name=release, download_client="SABnzbd", severity="error",
            reason="Arr observed SABnzbd reporting the tracked download as failed.",
            message=message, details={"evidence": "debug", "client_state": "failed"},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    if low_component == "redownloadfaileddownloadservice" and ARR_REDOWNLOAD_RE.search(message):
        recent = state.get("last_failed_download")
        release = None
        if isinstance(recent, dict):
            rdt = recent.get("time")
            if isinstance(rdt, datetime) and timedelta(0) <= current_dt - rdt <= timedelta(minutes=5):
                release = str(recent.get("release") or "").strip() or None
        state["retry_until"] = current_dt + timedelta(minutes=5)
        state["retry_release"] = release
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="retry_search_intent",
            search_mode="automatic_retry", origin_confidence="high",
            release_name=release, download_client="SABnzbd", severity="info",
            message=message, details={"evidence": "debug", "trigger": "failed_download"},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    mi = ARR_MARK_IMPORTED_RE.search(message)
    if low_component == "downloadeventhub" and mi:
        release = mi.group("release").strip()
        state["last_imported_download"] = {"time": current_dt, "release": release}
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="import_succeeded",
            release_name=release, download_client="SABnzbd", severity="info",
            message=message, details={"evidence": "debug", "signal": "marking_download_imported"},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    rh = ARR_REMOVE_HISTORY_RE.search(message)
    if low_component == "downloadeventhub" and rh:
        release = rh.group("release").strip()
        removal_reason = "unknown_removal"
        recent_import = state.get("last_imported_download")
        recent_failed = state.get("last_failed_download")
        if isinstance(recent_import, dict):
            rdt = recent_import.get("time")
            rr = str(recent_import.get("release") or "")
            if isinstance(rdt, datetime) and normalized_match_title(rr) == normalized_match_title(release) and timedelta(0) <= current_dt - rdt <= timedelta(minutes=5):
                removal_reason = "arr_remove_completed_history"
        if removal_reason == "unknown_removal" and isinstance(recent_failed, dict):
            rdt = recent_failed.get("time")
            rr = str(recent_failed.get("release") or "")
            if isinstance(rdt, datetime) and normalized_match_title(rr) == normalized_match_title(release) and timedelta(0) <= current_dt - rdt <= timedelta(minutes=5):
                removal_reason = "arr_remove_failed_history"
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="history_removed",
            release_name=release, download_client="SABnzbd", severity="info",
            reason=removal_reason, message=message,
            details={"evidence": "debug", "removal_reason": removal_reason},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    qd = ARR_QUEUE_DELETE_API_RE.search(message)
    if low_component == "api" and qd:
        query_text = qd.group("query")
        query = urllib.parse.parse_qs(query_text, keep_blank_values=True)
        if str((query.get("removeFromClient") or [""])[0]).lower() == "true":
            recent_retry = state.get("retry_until")
            removal_reason = "arr_failed_download_cleanup" if isinstance(recent_retry, datetime) and current_dt <= recent_retry else "arr_queue_remove_request"
            inserted += int(_insert_arr_event(
                conn, ts=ts, app=app, component=component, event_type="queue_removal_requested",
                download_client="SABnzbd", severity="info", reason=removal_reason,
                message=message,
                details={
                    "evidence": "debug",
                    "removal_reason": removal_reason,
                    "blocklist": str((query.get("blocklist") or [""])[0]).lower() == "true",
                    "skip_redownload": str((query.get("skipRedownload") or [""])[0]).lower() == "true",
                },
                segment_id=segment_id, raw_line=line,
            ))
            return inserted

    # Strong orchestration markers. Radarr logs an explicit parent automatic-search
    # event; Sonarr usually proves automatic searches when Episode/Season search
    # completion is emitted later. Standalone ReleaseSearchService activity is
    # left unknown here and classified as interactive/browse at query time.
    if low_component == "moviesearchservice" and ARR_AUTO_SEARCH_BEGIN_RE.search(message):
        state["automatic_until"] = datetime.fromisoformat(ts) + timedelta(seconds=10)
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="search_intent",
            search_mode="automatic", origin_confidence="high", severity=severity,
            message=message, details={"scope": "movie"}, segment_id=segment_id, raw_line=line,
        ))
        return inserted

    if low_component == "rsssyncservice" and ARR_RSS_START_RE.search(message):
        state["rss_until"] = datetime.fromisoformat(ts) + timedelta(minutes=10)
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="rss_started",
            search_mode="rss", origin_confidence="high", severity=severity,
            message=message, segment_id=segment_id, raw_line=line,
        ))
        return inserted

    if low_component == "rsssyncservice" and ARR_RSS_COMPLETE_RE.search(message):
        dm = ARR_RSS_COMPLETE_DETAIL_RE.search(message)
        details = {}
        if dm:
            details = {
                "reports_found": int(dm.group("found")),
                "reports_grabbed": int(dm.group("grabbed")),
                "reports_pending": int(dm.group("pending") or 0),
            }
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="rss_completed",
            search_mode="rss", origin_confidence="high", severity=severity,
            message=message, details=details, segment_id=segment_id, raw_line=line,
        ))
        return inserted

    sm = ARR_SEARCH_RE.search(message)
    if component.lower() == "releasesearchservice" and sm:
        media, season, episode = _arr_media_parts(app, sm.group("media"))
        current_dt = datetime.fromisoformat(ts)
        search_mode = "unknown"
        confidence = "none"
        retry_until = state.get("retry_until")
        retry_release = str(state.get("retry_release") or "").strip()
        auto_until = state.get("automatic_until")
        # Retry state is application-global in memory, but the evidence is media-specific.
        # Never let a failed Tulsa King download classify an unrelated House of the
        # Dragon search that merely happened inside the five-minute retry window.
        retry_matches_media = bool(
            retry_release
            and arr_media_matches_release(app, media, season, episode, retry_release)
        )
        has_auto_intent = isinstance(auto_until, datetime) and current_dt <= auto_until
        if isinstance(retry_until, datetime) and current_dt <= retry_until and retry_matches_media:
            search_mode, confidence = "automatic_retry", "high"
        elif has_auto_intent:
            # MovieSearchService emits the parent automatic-search marker immediately
            # before the ReleaseSearchService query.  Treat it as a one-shot intent,
            # not a five-minute mode window: an interactive search can begin seconds
            # after a completed automatic search for the same movie.
            search_mode, confidence = "automatic", "high"
        if has_auto_intent:
            # Consume the parent marker even when stronger retry evidence wins.
            # Otherwise the marker could leak into a second search a few seconds later.
            state["automatic_until"] = None
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="search_started",
            search_mode=search_mode, origin_confidence=confidence,
            media_title=media, season=season, episode=episode, severity=severity,
            message=message, details={"active_indexers": int(sm.group("count"))},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    dm = ARR_DECISION_PROCESS_RE.search(message)
    if low_component == "downloaddecisionmaker" and dm:
        current_dt = datetime.fromisoformat(ts)
        lower_bound = (current_dt - timedelta(minutes=5)).isoformat(timespec="seconds")
        row = conn.execute(
            """SELECT event_id,media_title,season,episode,search_mode,origin_confidence
               FROM arr_events WHERE app=? AND event_type='search_started'
                 AND event_time<=? AND event_time>=? ORDER BY event_time DESC LIMIT 1""",
            (app, ts, lower_bound),
        ).fetchone()
        if row is not None:
            # Keep this as internal evidence. At end-of-run, searches with decision
            # processing but no automatic completion are conservatively classified
            # as interactive/browse with medium confidence.
            inserted += int(_insert_arr_event(
                conn, ts=ts, app=app, component=component, event_type="search_results_processed",
                search_mode=str(row["search_mode"] or "unknown"),
                origin_confidence=str(row["origin_confidence"] or "none"),
                media_title=row["media_title"], season=row["season"], episode=row["episode"],
                severity=severity, message=message, details={"releases_processed": int(dm.group("count"))},
                segment_id=segment_id, raw_line=line,
            ))
        return inserted

    prm = ARR_PROCESS_RELEASE_RE.search(message)
    if low_component == "downloaddecisionmaker" and prm:
        search_row = _nearest_arr_search(conn, app, ts)
        if search_row is not None:
            cid = _upsert_search_candidate(
                conn, search_row=search_row, ts=ts, app=app,
                indexer=prm.group("indexer"), release=prm.group("release"),
            )
            state["current_candidate"] = {
                "candidate_id": cid, "search_event_id": search_row["event_id"],
                "release": prm.group("release").strip(),
                "indexer": re.sub(r"\s*\(Prowlarr\)\s*$", "", prm.group("indexer").strip(), flags=re.I),
                "media_title": search_row["media_title"], "season": search_row["season"], "episode": search_row["episode"],
                "time": current_dt,
            }
        return inserted

    if low_component == "parser":
        current = state.get("current_candidate")
        if isinstance(current, dict):
            cdt = current.get("time")
            if isinstance(cdt, datetime) and timedelta(0) <= current_dt - cdt <= timedelta(seconds=30):
                mpm = ARR_MOVIE_PARSED_RE.search(message) if app == "radarr" else None
                epm = ARR_EPISODE_PARSED_RE.search(message) if app == "sonarr" else None
                if mpm:
                    parsed_title = mpm.group("title").strip()
                    match = _parsed_media_matches_search(
                        app, current.get("media_title"), current.get("season"), current.get("episode"), parsed_title
                    )
                    conn.execute(
                        """UPDATE arr_search_candidates SET parsed_media_title=?,media_match=?,updated_at=?
                           WHERE candidate_id=?""",
                        (parsed_title, int(match), now_iso(), current["candidate_id"]),
                    )
                    return inserted
                if epm:
                    parsed_title = epm.group("title").strip()
                    parsed_season = int(epm.group("season"))
                    parsed_episode = int(epm.group("episode")) if epm.group("episode") else None
                    match = _parsed_media_matches_search(
                        app, current.get("media_title"), current.get("season"), current.get("episode"),
                        parsed_title, parsed_season, parsed_episode,
                    )
                    conn.execute(
                        """UPDATE arr_search_candidates SET parsed_media_title=?,parsed_season=?,parsed_episode=?,media_match=?,updated_at=?
                           WHERE candidate_id=?""",
                        (parsed_title, parsed_season, parsed_episode, int(match), now_iso(), current["candidate_id"]),
                    )
                    return inserted
                qpm = ARR_QUALITY_PARSED_RE.search(message)
                if qpm:
                    quality_raw = qpm.group("quality").strip()
                    quality_name = _quality_name(quality_raw, current.get("release"))
                    conn.execute(
                        """UPDATE arr_search_candidates SET quality_raw=?,quality_name=?,quality_tier=?,updated_at=?
                           WHERE candidate_id=?""",
                        (quality_raw, quality_name, _quality_tier(quality_name or quality_raw, current.get("release")),
                         now_iso(), current["candidate_id"]),
                    )
                    return inserted

    ajm = ARR_RELEASE_ACCEPTED_RE.search(message)
    if low_component == "downloaddecisionmaker" and ajm:
        search_row = _nearest_arr_search(conn, app, ts)
        if search_row is not None:
            _upsert_search_candidate(
                conn, search_row=search_row, ts=ts, app=app,
                indexer=ajm.group("indexer"), release=ajm.group("release"),
                decision="accepted",
            )
        current = state.get("current_candidate")
        if isinstance(current, dict) and normalized_match_title(current.get("release") or "") == normalized_match_title(ajm.group("release")):
            state.pop("current_candidate", None)
        return inserted

    rjm = ARR_RELEASE_REJECTED_RE.search(message)
    if low_component == "downloaddecisionmaker" and rjm:
        search_row = _nearest_arr_search(conn, app, ts)
        if search_row is not None:
            _upsert_search_candidate(
                conn, search_row=search_row, ts=ts, app=app,
                indexer=rjm.group("indexer"), release=rjm.group("release"),
                decision="rejected", rejection_reasons=rjm.group("reasons").strip(),
            )
        current = state.get("current_candidate")
        if isinstance(current, dict) and normalized_match_title(current.get("release") or "") == normalized_match_title(rjm.group("release")):
            state.pop("current_candidate", None)
        return inserted

    qm = ARR_QUEUE_RE.search(message)
    if component.lower() == "sabnzbd" and qm:
        release = qm.group("release").strip()
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="queued_to_sab",
            release_name=release, download_client="SABnzbd", severity=severity,
            message=message, segment_id=segment_id, raw_line=line,
        ))
        return inserted

    if component.lower() == "downloadservice":
        sm = ARR_SONARR_SENT_RE.search(message) if app == "sonarr" else None
        rm = ARR_RADARR_SENT_RE.search(message) if app == "radarr" else None
        if sm:
            inserted += int(_insert_arr_event(
                conn, ts=ts, app=app, component=component, event_type="sent_to_sab",
                release_name=sm.group("release").strip(), indexer=sm.group("indexer").strip(),
                download_client="SABnzbd", severity=severity, message=message,
                segment_id=segment_id, raw_line=line,
            ))
            search_row = _nearest_arr_search(conn, app, ts)
            if search_row is not None:
                _upsert_search_candidate(
                    conn, search_row=search_row, ts=ts, app=app, indexer=sm.group("indexer"),
                    release=sm.group("release").strip(), decision="selected",
                )
            return inserted
        if rm:
            media, season, episode = _arr_media_parts(app, rm.group("media"))
            inserted += int(_insert_arr_event(
                conn, ts=ts, app=app, component=component, event_type="sent_to_sab",
                media_title=media, season=season, episode=episode,
                release_name=rm.group("release").strip(), indexer=rm.group("indexer").strip(),
                download_client="SABnzbd", severity=severity, message=message,
                segment_id=segment_id, raw_line=line,
            ))
            search_row = _nearest_arr_search(conn, app, ts)
            if search_row is not None:
                _upsert_search_candidate(
                    conn, search_row=search_row, ts=ts, app=app, indexer=rm.group("indexer"),
                    release=rm.group("release").strip(), decision="selected",
                )
            return inserted

    gm = ARR_API_GRAB_LIMIT_RE.search(message)
    if component.lower() == "sabnzbd" and gm:
        raw_url = gm.group("url")
        try:
            parsed = urllib.parse.urlparse(raw_url)
            query = urllib.parse.parse_qs(parsed.query)
            release = urllib.parse.unquote((query.get("file") or [""])[0]).strip()
        except Exception:
            release = ""
        reason = "Indexer/API grab rate limit reached before the release could be sent to SABnzbd."
        if release:
            recent_grab_errors[normalized_match_title(release)] = (
                datetime.fromisoformat(ts), reason
            )
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="grab_error",
            release_name=release or None, download_client="SABnzbd", severity="error",
            reason=reason, message=message, details={"category": "rate_limit"},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    cm = ARR_COULDNT_ADD_RE.search(message)
    if cm:
        release = cm.group("release").strip()
        key = normalized_match_title(release)
        reason = "Release could not be added to the download queue."
        recent = recent_grab_errors.get(key)
        if recent:
            try:
                current_dt = datetime.fromisoformat(ts)
                if timedelta(0) <= current_dt - recent[0] <= timedelta(seconds=60):
                    reason = recent[1]
            except ValueError:
                pass
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="grab_failed",
            release_name=release, indexer=cm.group("indexer").strip(),
            download_client="SABnzbd", severity="error", reason=reason,
            message=message, segment_id=segment_id, raw_line=line,
        ))
        return inserted

    mm = ARR_MOVIE_ASSIGN_RE.search(message)
    if app == "radarr" and component.lower() == "movieservice" and mm:
        media, season, episode = _arr_media_parts(app, mm.group("media"))
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="import_succeeded",
            media_title=media, season=season, episode=episode, severity="info",
            message=message, details={"file": mm.group("file").strip()},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    # Import debug logs are extremely verbose, so only retain terminal import
    # signals from components that own the completed-download import workflow.
    # Generic Parser/ImportDecisionMaker messages such as "rejected" or
    # "imported" describe candidate-file decisions and must never become a
    # movie/episode-level terminal import result.
    terminal_import_components = {
        "downloadedepisodesimportservice",
        "downloadedmovieimportservice",
        "completeddownloadservice",
        "importapprovedepisodes",
        "importapprovedmovie",
    }
    if low_component in terminal_import_components and ARR_IMPORT_CANCEL_RE.search(message):
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="import_cancelled",
            severity="warn", reason=message, message=message,
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    strong_import_failure = any(phrase in low_message for phrase in (
        "import failed",
        "failed to import",
        "unable to import",
        "no files found are eligible for import",
        "unable to parse file on import",
    ))
    if low_component in terminal_import_components and strong_import_failure:
        # Radarr can emit auxiliary-file warnings for Featurettes/Extras/Samples
        # after the main movie file was already assigned successfully. Those are
        # not movie-level import failures.
        extra_path = bool(re.search(r"(?i)[\/](?:featurettes?|extras?|samples?|trailers?|behind[ ._-]*the[ ._-]*scenes?)[\/]", message))
        if extra_path:
            return inserted
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="import_failed",
            severity="error", reason=message, message=message,
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    # Success is deliberately handled only by the exact DownloadEventHub
    # "Marking download as imported" rule above and Radarr MovieService
    # assignment rule. Do not infer success from generic debug import chatter.

    if low_component in {"downloadmonitoringservice", "trackeddownloadservice", "trackeddownloadstate"} and ARR_DOWNLOAD_CANCEL_RE.search(message):
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="download_cancelled",
            severity="warn", reason=message, message=message,
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    sc = ARR_SEARCH_COMPLETE_RE.search(message)
    if sc and component.lower() in {
        "episodesearchservice", "seasonsearchservice", "moviesearchservice"
    }:
        # A matching Episode/Season/Movie completion is strong evidence that the
        # preceding ReleaseSearchService query was an automatic search rather
        # than an interactive result browse.
        current_dt = datetime.fromisoformat(ts)
        lower_bound = (current_dt - timedelta(minutes=20)).isoformat(timespec="seconds")
        row = conn.execute(
            """SELECT event_id,media_title,season,episode,search_mode,origin_confidence FROM arr_events
               WHERE app=? AND event_type='search_started' AND event_time<=? AND event_time>=?
               ORDER BY event_time DESC LIMIT 1""",
            (app, ts, lower_bound),
        ).fetchone()
        completed_mode = "automatic"
        if row is not None and str(row["search_mode"] or "").lower() == "automatic_retry":
            completed_mode = "automatic_retry"
        if row is not None:
            conn.execute(
                "UPDATE arr_events SET search_mode=?, origin_confidence='high' WHERE event_id=?",
                (completed_mode, row["event_id"]),
            )
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="search_completed",
            search_mode=completed_mode, origin_confidence="high",
            media_title=row["media_title"] if row is not None else None,
            season=row["season"] if row is not None else None,
            episode=row["episode"] if row is not None else None,
            severity=severity, message=message,
            details={"reports_downloaded": int(sc.group("count"))},
            segment_id=segment_id, raw_line=line,
        ))
        return inserted

    if component.lower() in {"downloadmonitoringservice", "radarrerrorpipeline", "sonarrerrorpipeline"} and ARR_SAB_UNREACHABLE_RE.search(message):
        inserted += int(_insert_arr_event(
            conn, ts=ts, app=app, component=component, event_type="sab_unreachable",
            download_client="SABnzbd", severity="error", reason=message, message=message,
            segment_id=segment_id, raw_line=line,
        ))
    return inserted


def reclassify_arr_search_origins(conn: sqlite3.Connection, app: str | None = None) -> int:
    """Classify retained Arr search origin from explicit orchestration evidence.

    Priority:
    1. ``RedownloadFailedDownloadService ... searching again`` -> Automatic retry/high.
    2. Immediate MovieSearchService parent intent or Episode/Season/Movie completion
       -> Automatic/high, unless the search was already tied to a failed-download retry.
    3. Mature result-processing + a release hand-off but no automatic completion ->
       Interactive/high (manual release selection observed).
    4. Mature result-processing with no hand-off/completion -> Interactive/medium
       (interactive browsing only).
    Anything else remains unknown.
    """
    params: tuple[Any, ...] = ()
    app_filter = ""
    if app:
        app_filter = " AND app=?"
        params = (app.lower(),)

    rows = conn.execute(
        f"""SELECT event_id,event_time,app,event_type,media_title,season,episode,release_name,
                   search_mode,origin_confidence
            FROM arr_events
            WHERE event_type IN (
                'search_intent','search_started','search_completed','search_results_processed',
                'retry_search_intent','download_failed_observed','sent_to_sab'
            ){app_filter}
            ORDER BY app,event_time""",
        params,
    ).fetchall()

    by_app: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_app[str(row["app"] or "").lower()].append(row)

    changed = 0

    def parsed_dt(row: sqlite3.Row) -> datetime | None:
        try:
            dt = datetime.fromisoformat(str(row["event_time"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt
        except (TypeError, ValueError):
            return None

    def same_search_context(a: sqlite3.Row, b: sqlite3.Row) -> bool:
        at = normalized_match_title(str(a["media_title"] or ""))
        bt = normalized_match_title(str(b["media_title"] or ""))
        if at and bt and at != bt:
            return False
        if a["season"] is not None and b["season"] is not None and int(a["season"]) != int(b["season"]):
            return False
        if a["episode"] is not None and b["episode"] is not None and int(a["episode"]) != int(b["episode"]):
            return False
        return True

    for app_name, events in by_app.items():
        parsed = [(dt, row) for row in events if (dt := parsed_dt(row)) is not None]
        if not parsed:
            continue
        latest_dt = max(dt for dt, _ in parsed)
        # A search session can mature even when no later *retained* orchestration
        # event exists (common after an interactive result browse). Use wall-clock
        # age as an additional maturity watermark; this leaves very recent sessions
        # unknown until a later collector run rather than guessing immediately.
        now_dt = local_now()
        maturity_dt = max(latest_dt, now_dt)
        automatic_intents = [(dt, row) for dt, row in parsed if row["event_type"] == "search_intent"]
        searches = [(dt, row) for dt, row in parsed if row["event_type"] == "search_started"]
        completions = [(dt, row) for dt, row in parsed if row["event_type"] == "search_completed"]
        processed = [(dt, row) for dt, row in parsed if row["event_type"] == "search_results_processed"]
        retry_intents = [(dt, row) for dt, row in parsed if row["event_type"] == "retry_search_intent"]
        failed_downloads = [(dt, row) for dt, row in parsed if row["event_type"] == "download_failed_observed"]
        sends = [(dt, row) for dt, row in parsed if row["event_type"] == "sent_to_sab"]

        # Explicit failed-download redownload intent is the strongest retry signal,
        # but it is still media-specific. Older retained retry-intent rows can lack
        # release_name when parser state crossed a rotation boundary. In that case,
        # recover the release only from the nearest preceding explicit failed-download
        # observation. If no media anchor survives, do not infer a retry.
        anchored_retry_search_ids: set[str] = set()
        for rdt, retry in retry_intents:
            retry_release = str(retry["release_name"] or "").strip()
            if not retry_release:
                prior_failed = [
                    (fdt, frow) for fdt, frow in failed_downloads
                    if timedelta(0) <= rdt - fdt <= timedelta(minutes=5)
                    and str(frow["release_name"] or "").strip()
                ]
                if prior_failed:
                    _, frow = max(prior_failed, key=lambda x: x[0])
                    retry_release = str(frow["release_name"] or "").strip()
            if not retry_release:
                continue
            candidates = [
                (sdt, srow) for sdt, srow in searches
                if timedelta(0) <= sdt - rdt <= timedelta(minutes=5)
                and arr_media_matches_release(
                    app_name, srow["media_title"], srow["season"], srow["episode"], retry_release
                )
            ]
            if not candidates:
                continue
            _, srow = min(candidates, key=lambda x: x[0])
            anchored_retry_search_ids.add(str(srow["event_id"]))

        # Rebuilds preserve Arr orchestration rows, so a bad retry label written by an
        # older collector can survive even when the current parser correctly replays
        # the logs. Remove stale retry labels that cannot be re-proven from retained
        # media-specific failed-download evidence, then let automatic/interactive
        # classification below reconstruct the proper origin.
        for _, srow in searches:
            if str(srow["search_mode"] or "").lower() != "automatic_retry":
                continue
            if str(srow["event_id"]) in anchored_retry_search_ids:
                continue
            cur = conn.execute(
                """UPDATE arr_events SET search_mode='unknown',origin_confidence='none'
                   WHERE event_id=? AND COALESCE(search_mode,'')='automatic_retry'""",
                (srow["event_id"],),
            )
            changed += int(cur.rowcount or 0)

        for _, srow in searches:
            if str(srow["event_id"]) not in anchored_retry_search_ids:
                continue
            cur = conn.execute(
                """UPDATE arr_events
                   SET search_mode='automatic_retry',origin_confidence='high'
                   WHERE event_id=? AND (
                       COALESCE(search_mode,'')<>'automatic_retry'
                       OR COALESCE(origin_confidence,'')<>'high'
                   )""",
                (srow["event_id"],),
            )
            changed += int(cur.rowcount or 0)

        # Establish ordinary automatic origin from structural Arr evidence, not from
        # a long-lived timing window.  Radarr's MovieSearchService parent marker is
        # emitted immediately before its ReleaseSearchService query; bind each parent
        # intent one-to-one to the first following search.  Sonarr/Radarr completion
        # markers remain strong evidence as well.  This distinguishes a later manual
        # Interactive search (for example Transcendence at 14:37:55) from the automatic
        # search that completed seconds earlier.
        automatic_search_ids: set[str] = set()
        claimed_search_ids: set[str] = set()
        for idt, intent in automatic_intents:
            candidates = [
                (sdt, srow) for sdt, srow in searches
                if str(srow["event_id"]) not in claimed_search_ids
                and timedelta(0) <= sdt - idt <= timedelta(seconds=10)
            ]
            if not candidates:
                continue
            _, srow = min(candidates, key=lambda x: x[0])
            sid = str(srow["event_id"])
            automatic_search_ids.add(sid)
            claimed_search_ids.add(sid)

        for cdt, completion in completions:
            candidates = [
                (sdt, srow) for sdt, srow in searches
                if timedelta(0) <= cdt - sdt <= timedelta(minutes=20)
                and same_search_context(srow, completion)
            ]
            if not candidates:
                continue
            _, srow = max(candidates, key=lambda x: x[0])
            automatic_search_ids.add(str(srow["event_id"]))

        # Older collectors could leave an 'automatic' label behind because a parent
        # MovieSearchService window stayed open for minutes.  Rebuilds preserve Arr
        # rows, so clear any ordinary automatic label that cannot be re-proven by a
        # parent intent or completion.  Explicit retries are handled separately above.
        for _, srow in searches:
            sid = str(srow["event_id"])
            current = conn.execute(
                "SELECT search_mode FROM arr_events WHERE event_id=?", (srow["event_id"],)
            ).fetchone()
            mode = str((current["search_mode"] if current else srow["search_mode"]) or "unknown").lower()
            if mode != "automatic" or sid in automatic_search_ids:
                continue
            cur = conn.execute(
                """UPDATE arr_events SET search_mode='unknown',origin_confidence='none'
                   WHERE event_id=? AND COALESCE(search_mode,'')='automatic'""",
                (srow["event_id"],),
            )
            changed += int(cur.rowcount or 0)

        for _, srow in searches:
            sid = str(srow["event_id"])
            if sid not in automatic_search_ids or sid in anchored_retry_search_ids:
                continue
            cur = conn.execute(
                """UPDATE arr_events SET search_mode='automatic',origin_confidence='high'
                   WHERE event_id=? AND (
                       COALESCE(search_mode,'')<>'automatic'
                       OR COALESCE(origin_confidence,'')<>'high'
                   )""",
                (srow["event_id"],),
            )
            changed += int(cur.rowcount or 0)

        # Mature sessions that still have no automatic/retry proof can be classified
        # from result-processing plus whether an actual release hand-off followed.
        for sdt, srow in searches:
            refreshed = conn.execute(
                "SELECT search_mode FROM arr_events WHERE event_id=?", (srow["event_id"],)
            ).fetchone()
            existing_mode = str((refreshed["search_mode"] if refreshed else srow["search_mode"]) or "unknown").lower()
            if existing_mode in {"automatic", "automatic_retry", "rss"}:
                continue
            if maturity_dt - sdt < timedelta(minutes=2):
                continue

            pcands = [
                (pdt, prow) for pdt, prow in processed
                if timedelta(0) <= pdt - sdt <= timedelta(minutes=5)
                and same_search_context(srow, prow)
            ]
            if not pcands:
                continue

            has_completion = any(
                timedelta(0) <= cdt - sdt <= timedelta(minutes=20)
                and same_search_context(srow, crow)
                for cdt, crow in completions
            )
            if has_completion:
                continue

            handoffs = [
                (hdt, hrow) for hdt, hrow in sends
                if timedelta(0) <= hdt - sdt <= timedelta(minutes=30)
                and (
                    (
                        hrow["release_name"]
                        and arr_media_matches_release(
                            app_name, srow["media_title"], srow["season"], srow["episode"],
                            str(hrow["release_name"])
                        )
                    )
                    or (
                        srow["media_title"] and hrow["media_title"]
                        and same_search_context(srow, hrow)
                    )
                )
            ]
            confidence = "high" if handoffs else "medium"
            cur = conn.execute(
                """UPDATE arr_events SET search_mode='interactive',origin_confidence=?
                   WHERE event_id=? AND COALESCE(search_mode,'unknown') IN ('','unknown','interactive')""",
                (confidence, srow["event_id"]),
            )
            changed += int(cur.rowcount or 0)

    return changed


def parse_arr_log_family(conn: sqlite3.Connection, source: Path, app: str) -> dict[str, Any]:
    run_started = now_iso()
    previous_collected_at = _latest_collection_time(
        conn, "arr_log_family_runs", "app=?", (app.lower(),)
    )
    """Parse Sonarr/Radarr active + numbered rotations using content identity."""
    app = app.strip().lower()
    if app not in {"sonarr", "radarr"}:
        raise ValueError("app must be sonarr or radarr")
    family = discover_numbered_text_logs(source, app, include_debug=True)
    segments_read = 0
    new_segments = 0
    lines_read = 0
    events = 0
    reset_detected = False
    recent_grab_errors: dict[str, tuple[datetime, str]] = {}
    state: dict[str, Any] = {}

    for path in family:
        stat = path.stat()
        segment_kind = f"{app}-debug-log-segment" if ".debug" in path.name.lower() else f"{app}-log-segment"
        segment_id, first_ts, line_hash = _generic_segment_id(segment_kind, path, ARR_LINE_RE)
        offset, reset, is_new = _arr_segment_offset(conn, app, segment_id, stat.st_size)
        if is_new:
            new_segments += 1
        reset_detected = reset_detected or reset

        end = offset
        segment_lines = 0
        with path.open("rb") as f:
            f.seek(offset)
            for raw in complete_lines(f):
                segment_lines += 1
                lines_read += 1
                end = f.tell()
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                events += _parse_arr_line(
                    conn, app, line, segment_id, recent_grab_errors, state
                )
        if segment_lines:
            segments_read += 1
        _set_arr_segment_state(
            conn, app, segment_id, first_ts, line_hash, path, end, stat.st_size
        )

    live_events, backfill_events = _split_new_rows_by_cutoff(
        conn, "arr_events", "event_time", run_started, previous_collected_at, "app=?", (app,)
    )
    conn.execute(
        """
        INSERT INTO arr_log_family_runs(
            collected_at,app,root_path,segments_found,segments_read,new_segments,
            lines_read,relevant_events,live_events,backfill_events,reset_detected
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(), app, str(source.resolve()), len(family), segments_read,
            new_segments, lines_read, events, live_events, backfill_events, int(reset_detected),
        ),
    )
    return {
        "app": app,
        "segments_found": len(family),
        "segments_read": segments_read,
        "new_segments": new_segments,
        "lines": lines_read,
        "events": events,
        "live_events": live_events,
        "backfill_events": backfill_events,
        "reset": reset_detected,
    }


def backfill_search_coverage_evidence(
    conn: sqlite3.Connection,
    prow_source: Path,
    known_indexer_names: list[str],
    sonarr_source: Path | None = None,
    radarr_source: Path | None = None,
) -> dict[str, int]:
    """One-time retained-log pass for raw search candidates and parsed qualities.

    v7.10.2 needs each Arr candidate exactly as it appeared before rejection, plus
    Arr's parsed quality and parsed media identity. Replaying retained normal/debug
    families once updates existing candidates in-place while semantic event
    de-duplication prevents duplicate Activity rows.
    """
    name = "raw_search_evidence_v2"
    row = conn.execute("SELECT details FROM collector_migrations WHERE name=?", (name,)).fetchone()
    if row is not None:
        try:
            data = json.loads(row["details"] or "{}")
            return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
        except Exception:
            return {"already_done": 1}

    stats = Counter()
    pc = Counter()
    per = Counter()
    for path in discover_numbered_text_logs(prow_source, "prowlarr", include_debug=True):
        with path.open("rb") as f:
            for raw in complete_lines(f):
                stats["prowlarr_lines"] += 1
                _process_prowlarr_line(
                    conn, raw.decode("utf-8", errors="replace").rstrip("\r\n"),
                    known_indexer_names, pc, per,
                )

    for app, source in (("sonarr", sonarr_source), ("radarr", radarr_source)):
        if source is None:
            continue
        state: dict[str, Any] = {}
        recent: dict[str, tuple[datetime, str]] = {}
        for path in discover_numbered_text_logs(source, app, include_debug=True):
            kind = f"{app}-coverage-backfill"
            seg_id, _, _ = _generic_segment_id(kind, path, ARR_LINE_RE)
            with path.open("rb") as f:
                for raw in complete_lines(f):
                    stats[f"{app}_lines"] += 1
                    _parse_arr_line(
                        conn, app, raw.decode("utf-8", errors="replace").rstrip("\r\n"),
                        seg_id, recent, state,
                    )

    stats["candidates"] = int(conn.execute("SELECT COUNT(*) n FROM arr_search_candidates").fetchone()["n"] or 0)
    stats["searches_with_counts"] = int(conn.execute("SELECT COUNT(*) n FROM prowlarr_log_searches WHERE result_count IS NOT NULL").fetchone()["n"] or 0)
    payload = dict(stats)
    conn.execute(
        "INSERT OR REPLACE INTO collector_migrations(name,applied_at,details) VALUES(?,?,?)",
        (name, now_iso(), json.dumps(payload, sort_keys=True)),
    )
    return payload


def _profile_leaf_qualities(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    quality = node.get("quality")
    if isinstance(quality, dict):
        name = str(quality.get("name") or "").strip()
        if name:
            return [name]
    leaves: list[str] = []
    for child in node.get("items") or []:
        leaves.extend(_profile_leaf_qualities(child))
    # Keep API order but remove duplicates.
    return list(dict.fromkeys(x for x in leaves if x))


def _find_profile_cutoff_item(items: Any, cutoff: Any) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    cutoff_text = str(cutoff if cutoff is not None else "").strip().lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        ids = {str(item.get("id") if item.get("id") is not None else "").strip().lower()}
        quality = item.get("quality")
        if isinstance(quality, dict):
            ids.add(str(quality.get("id") if quality.get("id") is not None else "").strip().lower())
            ids.add(str(quality.get("name") or "").strip().lower())
        ids.add(str(item.get("name") or "").strip().lower())
        if cutoff_text and cutoff_text in ids:
            return item
        child = _find_profile_cutoff_item(item.get("items") or [], cutoff)
        if child is not None:
            return child
    return None


def _profile_target_spec(profile: dict[str, Any]) -> dict[str, Any] | None:
    cutoff = profile.get("cutoff")
    item = _find_profile_cutoff_item(profile.get("items") or [], cutoff)
    if item is None:
        return None
    qualities = _profile_leaf_qualities(item)
    if not qualities:
        return None
    label = str(item.get("name") or "").strip()
    quality = item.get("quality")
    if not label and isinstance(quality, dict):
        label = str(quality.get("name") or "").strip()
    if not label:
        label = ", ".join(qualities)
    tiers = list(dict.fromkeys(t for t in (_quality_tier(q) for q in qualities) if t))
    return {
        "profile_id": profile.get("id"),
        "profile_name": str(profile.get("name") or "").strip(),
        "cutoff_id": cutoff,
        "target_label": label,
        "target_qualities": qualities,
        "target_tiers": tiers,
        "upgrade_allowed": bool(profile.get("upgradeAllowed", True)),
        "min_format_score": profile.get("minFormatScore"),
        "cutoff_format_score": profile.get("cutoffFormatScore"),
        "min_upgrade_format_score": profile.get("minUpgradeFormatScore"),
    }


def fetch_arr_profile_snapshot(app: str, base_url: str | None, api_key: str | None) -> dict[str, Any]:
    app = str(app or "").lower()
    captured_at = now_iso()
    if app not in {"sonarr", "radarr"}:
        raise ValueError("app must be sonarr or radarr")
    if not base_url or not api_key:
        return {"app": app, "configured": False, "captured_at": captured_at, "profiles": {}, "entities": {}}
    profiles_raw = arr_api_get(app, base_url, api_key, "/api/v3/qualityprofile")
    entity_path = "/api/v3/series" if app == "sonarr" else "/api/v3/movie"
    entities_raw = arr_api_get(app, base_url, api_key, entity_path)
    profiles: dict[int, dict[str, Any]] = {}
    if isinstance(profiles_raw, list):
        for profile in profiles_raw:
            if not isinstance(profile, dict):
                continue
            try:
                pid = int(profile.get("id"))
            except (TypeError, ValueError):
                continue
            spec = _profile_target_spec(profile)
            if spec:
                profiles[pid] = spec
    entities: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if isinstance(entities_raw, list):
        for entity in entities_raw:
            if not isinstance(entity, dict):
                continue
            title = str(entity.get("title") or "").strip()
            try:
                pid = int(entity.get("qualityProfileId"))
            except (TypeError, ValueError):
                continue
            if not title or pid not in profiles:
                continue
            key = normalized_match_title(title)
            entities[key].append({
                "title": title,
                "id": entity.get("id"),
                "quality_profile_id": pid,
                "profile": profiles[pid],
            })
    return {
        "app": app,
        "configured": True,
        "captured_at": captured_at,
        "profiles": profiles,
        "entities": dict(entities),
    }


def _quality_evidence_key(value: str | None) -> str:
    value = re.sub(r"\s+v\d+\s*$", "", str(value or ""), flags=re.I).strip()
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _arr_target_profile_conflicts(
    conn: sqlite3.Connection, search_event_id: str, target_qualities: list[str] | tuple[str, ...]
) -> tuple[bool, list[str]]:
    """Return whether an API target contradicts retained same-search Arr decisions.

    API profiles are mutable and the collector can run after a user temporarily
    changes a movie/series profile. A later 1080p snapshot must therefore never be
    back-applied to an earlier search whose own Radarr/Sonarr decision stream says
    those 1080p qualities were explicitly *not wanted in profile*.

    Some releases can produce apparently conflicting quality labels (for example a
    BR-DISK/full-disc release parsed under a generic Bluray-1080p label). Therefore
    one negative row is not enough to veto a snapshot when the same search also has
    positive profile-compatible evidence for that exact quality. A conflict exists
    only when a snapshot target quality is explicitly profile-rejected and has no
    same-search accepted/selected/otherwise-compatible counterpart.
    """
    target_keys = {_quality_evidence_key(q) for q in (target_qualities or [])}
    target_keys.discard("")
    if not target_keys:
        return False, []
    search = conn.execute(
        """SELECT app,media_title,season,episode FROM arr_events
           WHERE event_id=? AND event_type='search_started'""",
        (str(search_event_id),),
    ).fetchone()
    if search is None:
        return False, []

    rejected: dict[str, str] = {}
    positive: set[str] = set()
    rows = conn.execute(
        """SELECT quality_name,quality_raw,decision,rejection_reasons,media_match,release_name
           FROM arr_search_candidates WHERE search_event_id=?""",
        (str(search_event_id),),
    ).fetchall()
    for row in rows:
        if row["media_match"] is not None:
            if int(row["media_match"] or 0) == 0:
                continue
        elif not arr_media_matches_release(
            str(search["app"] or ""), search["media_title"], search["season"], search["episode"],
            str(row["release_name"] or ""),
        ):
            continue
        quality = str(row["quality_name"] or row["quality_raw"] or "").strip()
        key = _quality_evidence_key(quality)
        if not key or key not in target_keys:
            continue
        reasons = str(row["rejection_reasons"] or "")
        decision = str(row["decision"] or "considered").lower()
        not_wanted = "not wanted in profile" in reasons.lower()
        if not_wanted:
            rejected[key] = re.sub(r"\s+v\d+\s*$", "", quality, flags=re.I).strip()
        elif decision in {"accepted", "selected"} or (decision == "rejected" and bool(reasons)):
            positive.add(key)

    conflicts = [label for key, label in rejected.items() if key not in positive]
    return bool(conflicts), sorted(set(conflicts), key=str.lower)


def revalidate_arr_api_search_targets(conn: sqlite3.Connection) -> list[str]:
    """Remove mutable API snapshots disproven by same-search Arr decision evidence."""
    removed: list[str] = []
    rows = conn.execute(
        """SELECT search_event_id,target_qualities_json FROM arr_search_targets
           WHERE source='arr_api_snapshot'"""
    ).fetchall()
    for row in rows:
        try:
            qualities = json.loads(row["target_qualities_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            qualities = []
        conflict, _ = _arr_target_profile_conflicts(conn, str(row["search_event_id"]), qualities if isinstance(qualities, list) else [])
        if not conflict:
            continue
        conn.execute("DELETE FROM arr_search_targets WHERE search_event_id=?", (row["search_event_id"],))
        removed.append(str(row["search_event_id"]))
    return removed


def capture_arr_search_targets(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    fresh_after: str,
    max_age_hours: float = 6.0,
) -> int:
    """Attach the live Arr profile cutoff to newly observed searches only.

    Quality profiles are mutable. Applying today's profile to a week-old search
    would rewrite history (Transcendence is exactly why that is unsafe), so a
    snapshot is only attached to searches close to the time it was collected.
    """
    app = str(snapshot.get("app") or "").lower()
    if not snapshot.get("configured") or app not in {"sonarr", "radarr"}:
        return 0
    entities = snapshot.get("entities") or {}
    captured_at = parse_timestamp_any(str(snapshot.get("captured_at") or "")) or now_iso()
    captured_dt = datetime.fromisoformat(captured_at)
    rows = conn.execute(
        """SELECT e.event_id,e.event_time,e.app,e.media_title,e.season,e.episode
           FROM arr_events e
           LEFT JOIN arr_search_targets t ON t.search_event_id=e.event_id
           WHERE e.event_type='search_started' AND e.app=? AND e.event_time>=?
             AND t.search_event_id IS NULL
           ORDER BY e.event_time""",
        (app, fresh_after),
    ).fetchall()
    inserted = 0
    for row in rows:
        event_dt = datetime.fromisoformat(str(row["event_time"]))
        age_hours = (captured_dt - event_dt).total_seconds() / 3600.0
        if age_hours < -0.1 or age_hours > max_age_hours:
            continue
        key = normalized_match_title(str(row["media_title"] or ""))
        matches = entities.get(key) or []
        # Ambiguous duplicate titles are deliberately left unresolved rather than
        # silently applying one movie/series profile to another.
        profile_ids = {int(x["quality_profile_id"]) for x in matches if x.get("quality_profile_id") is not None}
        if len(matches) != 1 and len(profile_ids) != 1:
            continue
        entity = matches[0]
        spec = entity["profile"]
        conflict, rejected_targets = _arr_target_profile_conflicts(
            conn, str(row["event_id"]), list(spec.get("target_qualities") or [])
        )
        if conflict:
            # The search's own decision stream outranks a mutable API snapshot
            # captured later (for example a temporary 4K -> 1080p profile switch).
            continue
        confidence = "high" if age_hours <= 2.0 else "medium"
        details = {
            "entity_id": entity.get("id"),
            "entity_title": entity.get("title"),
            "snapshot_age_minutes": round(max(0.0, age_hours) * 60.0, 1),
            "upgrade_allowed": spec.get("upgrade_allowed"),
            "min_format_score": spec.get("min_format_score"),
            "cutoff_format_score": spec.get("cutoff_format_score"),
            "min_upgrade_format_score": spec.get("min_upgrade_format_score"),
        }
        cur = conn.execute(
            """INSERT OR IGNORE INTO arr_search_targets(
                   search_event_id,event_time,app,media_title,season,episode,profile_id,profile_name,cutoff_id,
                   target_label,target_qualities_json,target_tiers_json,source,confidence,captured_at,details_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["event_id"], row["event_time"], app, row["media_title"], row["season"], row["episode"],
                spec.get("profile_id"), spec.get("profile_name"), str(spec.get("cutoff_id") or ""),
                spec.get("target_label"), json.dumps(spec.get("target_qualities") or [], ensure_ascii=False),
                json.dumps(spec.get("target_tiers") or [], ensure_ascii=False), "arr_api_snapshot", confidence,
                snapshot.get("captured_at") or now_iso(), json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
        inserted += int(cur.rowcount > 0)
    return inserted


def infer_historical_arr_search_targets(
    conn: sqlite3.Connection, search_event_ids: set[str] | list[str] | tuple[str, ...] | None = None
) -> dict[str, int]:
    """Build historical wanted-quality evidence from retained Arr decisions.

    This is intentionally based on the quality-profile decision boundary rather than
    the final grab decision. A correct-media release still proves raw availability
    when Arr later rejects it for size, language, Custom Format score, retention or
    another downstream rule. The only quality-specific negative we exclude is Arr's
    explicit ``not wanted in profile`` reason.

    The inferred target set is therefore the set of *observed profile-compatible
    qualities* for that logical search. It does not pretend to reconstruct a mutable
    profile name/cutoff that was never logged. Exact API snapshots always win and are
    never overwritten.
    """
    wanted_ids = {str(x) for x in (search_event_ids or []) if str(x)}
    if search_event_ids is not None and not wanted_ids:
        return {"inserted": 0, "high": 0, "medium": 0, "unresolved": 0}
    if search_event_ids is None:
        searches = conn.execute(
            """SELECT event_id,event_time,app,media_title,season,episode,search_mode
               FROM arr_events WHERE event_type='search_started' ORDER BY event_time"""
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in wanted_ids)
        searches = conn.execute(
            f"""SELECT event_id,event_time,app,media_title,season,episode,search_mode
                FROM arr_events WHERE event_type='search_started'
                  AND event_id IN ({placeholders}) ORDER BY event_time""",
            tuple(sorted(wanted_ids)),
        ).fetchall()
    existing = {
        str(r[0]) for r in conn.execute("SELECT search_event_id FROM arr_search_targets")
    }
    stats = {"inserted": 0, "high": 0, "medium": 0, "unresolved": 0}

    def qkey(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    for search in searches:
        sid = str(search["event_id"])
        if sid in existing:
            continue
        rows = conn.execute(
            """SELECT quality_name,quality_raw,quality_tier,decision,rejection_reasons,media_match,indexer,release_name
               FROM arr_search_candidates
               WHERE search_event_id=? AND COALESCE(quality_name,quality_raw,'')<>''""",
            (sid,),
        ).fetchall()
        if not rows:
            stats["unresolved"] += 1
            continue

        allowed: dict[str, str] = {}
        allowed_tiers: set[str] = set()
        profile_rejected: dict[str, str] = {}
        selected: set[str] = set()
        accepted_count = 0
        other_reject_count = 0
        for row in rows:
            # Explicit wrong-media parses must never establish the searched profile.
            # When the parser identity is absent, fall back to the conservative title/
            # episode matcher rather than treating an unrelated Prowlarr result as
            # profile evidence.
            if row["media_match"] is not None:
                if int(row["media_match"] or 0) == 0:
                    continue
            elif not arr_media_matches_release(
                str(search["app"] or ""), search["media_title"], search["season"], search["episode"],
                str(row["release_name"] or ""),
            ):
                continue
            quality = str(row["quality_name"] or row["quality_raw"] or "").strip()
            if not quality:
                continue
            quality = re.sub(r"\s+v\d+\s*$", "", quality, flags=re.I).strip()
            key = qkey(quality)
            if not key:
                continue
            decision = str(row["decision"] or "considered").lower()
            reasons = str(row["rejection_reasons"] or "")
            not_wanted = "not wanted in profile" in reasons.lower()
            tier = str(row["quality_tier"] or _quality_tier(quality) or "").strip()

            if not_wanted:
                profile_rejected[key] = quality
                continue
            # Strong positive evidence: Arr explicitly accepted/selected it, or it
            # explicitly rejected it for *another* reason. In all of those cases the
            # quality-profile check itself was passed.
            if decision in {"accepted", "selected"} or (decision == "rejected" and reasons):
                allowed[key] = quality
                if tier:
                    allowed_tiers.add(tier)
                if decision == "selected":
                    selected.add(quality)
                if decision == "accepted":
                    accepted_count += 1
                elif decision == "rejected":
                    other_reject_count += 1

        # If the same exact quality somehow appeared with conflicting evidence in one
        # search, explicit profile rejection wins; do not score that quality.
        for key in list(allowed):
            if key in profile_rejected:
                allowed.pop(key, None)
        qualities = sorted(allowed.values(), key=lambda x: (_quality_tier(x) or "", x.lower()))
        if not qualities:
            stats["unresolved"] += 1
            continue
        # A quality profile may allow lower fallback qualities while its wanted/cutoff
        # tier is higher. Historical logs do not retain the mutable cutoff directly,
        # so score only the highest profile-compatible resolution tier observed in the
        # search. This prevents a 720p fallback from being counted as the target when
        # 1080p was also profile-compatible.
        tier_rank = {"SD": 0, "720p": 1, "1080p": 2, "2160p": 3}
        observed_tiers = [t for t in (_quality_tier(q) for q in qualities) if t in tier_rank]
        if observed_tiers:
            top_tier = max(observed_tiers, key=lambda t: tier_rank[t])
            qualities = [q for q in qualities if _quality_tier(q) == top_tier]
            tiers = [top_tier]
        else:
            tiers = []
        if len(qualities) == 1:
            label = qualities[0]
        elif len(tiers) == 1:
            label = f"{tiers[0]} profile (historical)"
        else:
            label = "Historical profile-compatible qualities"

        # Explicit rejected alternatives plus positive profile-compatible evidence is
        # the strongest historical reconstruction available from the logs. A selected
        # release is also strong evidence that its quality was wanted/allowed then.
        confidence = "high" if profile_rejected and (accepted_count or selected or other_reject_count) else "medium"
        details = {
            "method": "arr_decision_reconstruction",
            "allowed_qualities": qualities,
            "profile_rejected_qualities": sorted(profile_rejected.values()),
            "selected_qualities": sorted(selected),
            "accepted_candidates": accepted_count,
            "other_rejected_candidates": other_reject_count,
            "semantics": "observed profile-compatible qualities; downstream rejection reasons ignored",
        }
        conn.execute(
            """INSERT OR IGNORE INTO arr_search_targets(
                   search_event_id,event_time,app,media_title,season,episode,profile_id,profile_name,cutoff_id,
                   target_label,target_qualities_json,target_tiers_json,source,confidence,captured_at,details_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, search["event_time"], search["app"], search["media_title"], search["season"], search["episode"],
                None, None, None, label, json.dumps(qualities, ensure_ascii=False), json.dumps(tiers, ensure_ascii=False),
                "historical_arr_decision", confidence, now_iso(), json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
        stats["inserted"] += 1
        stats[confidence] += 1
        existing.add(sid)

    return stats


def save_arr_profile_snapshot_run(
    conn: sqlite3.Connection, snapshot: dict[str, Any], *, targets_applied: int = 0, error: str | None = None
) -> None:
    app = str(snapshot.get("app") or "arr")
    configured = int(bool(snapshot.get("configured")))
    profiles = len(snapshot.get("profiles") or {})
    entities = sum(len(v) for v in (snapshot.get("entities") or {}).values())
    status = "error" if error else "ok" if configured else "not_configured"
    conn.execute(
        """INSERT INTO arr_profile_snapshot_runs(
               collected_at,app,configured,profiles,entities,targets_applied,status,message
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (now_iso(), app, configured, profiles, entities, int(targets_applied), status, error),
    )


def print_arr_log_delta(info: dict[str, Any] | None) -> None:
    if not info:
        return
    label = str(info.get("app") or "arr").title()
    print(f"\n{label.upper()} LOG ACTIVITY")
    print("-" * 58)
    print(f"Segments found             : {int(info.get('segments_found', 0)):,}")
    print(f"Segments read this run     : {int(info.get('segments_read', 0)):,}")
    print(f"New segments               : {int(info.get('new_segments', 0)):,}")
    print(f"Lines read                 : {int(info.get('lines', 0)):,}")
    print(f"Relevant events imported   : {int(info.get('events', 0)):,}")
    if info.get("reset"):
        print("Rotation/truncation        : detected")


# ===========================================================================
# Correlation / reporting queries
# ===========================================================================

def sab_release_outcomes(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_time,job_name,status,max_repair_blocks FROM jobs ORDER BY event_time"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = normalized_match_title(str(r["job_name"]))
        if not key:
            continue
        g = out.setdefault(
            key,
            {"attempts": 0, "completed": False, "failed": False, "max_repair_blocks": 0},
        )
        g["attempts"] += 1
        g["completed"] |= str(r["status"]).lower() == "completed"
        g["failed"] |= str(r["status"]).lower() == "failed"
        g["max_repair_blocks"] = max(g["max_repair_blocks"], int(r["max_repair_blocks"] or 0))
    return out


def prowlarr_grab_rows(conn) -> list[sqlite3.Row]:
    # Event type strings can vary in capitalization/naming. Match obvious grab forms.
    return conn.execute(
        """
        SELECT event_time,indexer_id,indexer_name,event_type,release_title
        FROM prowlarr_history
        WHERE lower(event_type) LIKE '%grab%'
          AND COALESCE(release_title,'') <> ''
        ORDER BY event_time
        """
    ).fetchall()


def correlate_grabs(conn) -> dict[str, dict[str, int]]:
    outcomes = sab_release_outcomes(conn)
    result: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "grabs_with_title": 0,
            "matched": 0,
            "eventually_completed": 0,
            "failed_only": 0,
            "repair": 0,
            "heavy_repair": 0,
        }
    )
    for r in prowlarr_grab_rows(conn):
        indexer = str(r["indexer_name"] or r["indexer_id"])
        result[indexer]["grabs_with_title"] += 1
        key = normalized_match_title(str(r["release_title"]))
        if key not in outcomes:
            continue
        result[indexer]["matched"] += 1
        o = outcomes[key]
        if o["completed"]:
            result[indexer]["eventually_completed"] += 1
        elif o["failed"]:
            result[indexer]["failed_only"] += 1
        if o["max_repair_blocks"] > 0:
            result[indexer]["repair"] += 1
        if o["max_repair_blocks"] >= 10:
            result[indexer]["heavy_repair"] += 1
    return dict(result)


def job_stats(conn) -> tuple[int, int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
        FROM jobs
        """
    ).fetchone()
    return int(row["total"] or 0), int(row["completed"] or 0), int(row["failed"] or 0)


def unique_release_stats(conn) -> tuple[int, int, int, int]:
    rows = conn.execute(
        "SELECT job_name,status,event_time FROM jobs ORDER BY event_time"
    ).fetchall()
    grouped = defaultdict(list)
    for r in rows:
        grouped[normalized_release_identity(str(r["job_name"]))].append(str(r["status"]).lower())
    total = len(grouped)
    eventually = sum("completed" in s for s in grouped.values())
    failed_only = sum(("completed" not in s and "failed" in s) for s in grouped.values())
    recovered = 0
    for statuses in grouped.values():
        failed_seen = False
        for st in statuses:
            if st == "failed":
                failed_seen = True
            elif st == "completed" and failed_seen:
                recovered += 1
                break
    return total, eventually, failed_only, recovered



def latest_provider_deltas(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    providers = [
        r["provider"]
        for r in conn.execute("SELECT DISTINCT provider FROM provider_snapshots ORDER BY provider")
    ]
    out = []
    for provider in providers:
        rows = conn.execute(
            """
            SELECT id,collected_at,articles_tried,articles_success,day_bytes,week_bytes,total_bytes
            FROM provider_snapshots
            WHERE provider=?
            ORDER BY id DESC
            LIMIT 2
            """,
            (provider,),
        ).fetchall()
        if not rows:
            continue
        cur = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def delta(field: str) -> int:
            current = int(cur[field] or 0)
            if prev is None:
                return current
            previous = int(prev[field] or 0)
            return current - previous if current >= previous else current

        out.append({
            "provider": provider,
            "collected_at": cur["collected_at"],
            "tried_delta": delta("articles_tried"),
            "success_delta": delta("articles_success"),
            "day_bytes_delta": delta("day_bytes"),
            "week_bytes_delta": delta("week_bytes"),
            "total_bytes_delta": delta("total_bytes"),
        })
    return out


def latest_indexer_stat_deltas(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = [
        int(r["indexer_id"])
        for r in conn.execute(
            "SELECT DISTINCT indexer_id FROM prowlarr_stats_snapshots ORDER BY indexer_id"
        )
    ]
    out = []
    for idx in ids:
        rows = conn.execute(
            """
            SELECT id,indexer_name,queries,grabs,failed_queries,failed_grabs,failed_rss
            FROM prowlarr_stats_snapshots
            WHERE indexer_id=?
            ORDER BY id DESC
            LIMIT 2
            """,
            (idx,),
        ).fetchall()
        if not rows:
            continue
        cur = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def delta(field: str) -> int:
            current = int(cur[field] or 0)
            if prev is None:
                return current
            previous = int(prev[field] or 0)
            return current - previous if current >= previous else current

        out.append({
            "indexer": str(cur["indexer_name"]),
            "queries_delta": delta("queries"),
            "grabs_delta": delta("grabs"),
            "failed_queries_delta": delta("failed_queries"),
            "failed_grabs_delta": delta("failed_grabs"),
            "failed_rss_delta": delta("failed_rss"),
        })
    return out


def print_last_collection(
    conn: sqlite3.Connection,
    sab_delta: dict[str, Any],
    prow_log_delta: dict[str, Any],
) -> None:
    print("\nLAST COLLECTION DELTA")
    print("-" * 72)
    print(f"SAB live completed         : {int(sab_delta.get('live_completed', 0)):,}")
    print(f"SAB backfilled completed   : {int(sab_delta.get('backfill_completed', 0)):,}")
    print(f"SAB live failed            : {int(sab_delta.get('live_failed', 0)):,}")
    print(f"SAB backfilled failed      : {int(sab_delta.get('backfill_failed', 0)):,}")
    print(f"SAB new log lines          : {int(sab_delta.get('lines', 0)):,}")
    print(f"Unavailable article events : {int(sab_delta.get('unavailable', 0)):,}")
    print(f"Repair-related events      : {int(sab_delta.get('repair', 0)):,}")

    pd = latest_provider_deltas(conn)
    for row in pd:
        print(
            f"{row['provider']} data delta"
            f" : {human_bytes(row['day_bytes_delta'])} today-counter growth, "
            f"{row['tried_delta']:,} tried-counter growth, "
            f"{row['success_delta']:,} success-counter growth"
        )

    pc = prow_log_delta.get("counts", {})
    print(f"Prowlarr live searches     : {int(pc.get('live_searches', 0)):,}")
    print(f"Prowlarr backfill searches : {int(pc.get('backfill_searches', 0)):,}")
    print(f"Prowlarr invalid releases  : {int(pc.get('invalid_releases', 0)):,}")
    print(f"Prowlarr connection errors : {int(pc.get('connection_failures', 0)):,}")

    for row in latest_indexer_stat_deltas(conn):
        print(
            f"{row['indexer']:<24}: "
            f"+{row['queries_delta']:,} queries, "
            f"+{row['grabs_delta']:,} grabs"
        )


def print_provider(providers: list[ProviderStats]) -> None:
    print("\nSAB PROVIDER ARTICLE AVAILABILITY")
    print("-" * 128)
    print(
        f"{'Provider':<32}{'Requested':>12}{'Available':>12}{'Unavailable':>12}"
        f"{'Availability':>14}{'Priority':>10}{'Conns':>8}{'Today':>13}{'Week':>13}"
    )
    print("-" * 128)
    for p in providers:
        availability = "n/a" if p.availability is None else f"{p.availability:.3f}%"
        priority = "—" if p.priority is None else str(p.priority)
        connections = "—" if p.connections is None else str(p.connections)
        print(
            f"{p.name[:31]:<32}{p.tried:>12,}{p.available:>12,}{p.unavailable:>12,}"
            f"{availability:>14}{priority:>10}{connections:>8}"
            f"{human_bytes(p.day_bytes):>13}{human_bytes(p.week_bytes):>13}"
        )


def print_prowlarr_indexers(indexers, stat_rows) -> None:
    print("\nPROWLARR NZB INDEXER HEALTH")
    print("-" * 122)
    print(
        f"{'Indexer':<24}{'Queries':>10}{'Grabs':>9}{'Q fail':>9}{'Grab fail':>10}"
        f"{'RSS fail':>9}{'Query ms':>11}{'Grab ms':>10}{'Grab/Q':>9}"
    )
    print("-" * 122)
    by_id = {r["indexer_id"]: r for r in stat_rows}
    for idx, item in sorted(indexers.items(), key=lambda x: str(x[1].get("name","")).lower()):
        row = by_id.get(idx, {})
        q = int(row.get("queries", 0))
        g = int(row.get("grabs", 0))
        print(
            f"{str(item.get('name', idx))[:23]:<24}"
            f"{q:>10,}{g:>9,}"
            f"{int(row.get('failed_queries',0)):>9,}"
            f"{int(row.get('failed_grabs',0)):>10,}"
            f"{int(row.get('failed_rss',0)):>9,}"
            f"{float(row.get('avg_ms',0)):>11.1f}"
            f"{float(row.get('avg_grab_ms',0)):>10.1f}"
            f"{pct(g,q):>9}"
        )


def print_sab_summary(conn) -> None:
    attempts, comp, fail = job_stats(conn)
    unique, eventual, failed_only, recovered = unique_release_stats(conn)
    print("\nSAB OUTCOMES")
    print("-" * 58)
    print(f"Attempts stored            : {attempts:,}")
    print(f"Completed attempts         : {comp:,}")
    print(f"Failed attempts            : {fail:,}")
    print(f"Attempt success rate       : {pct(comp, attempts)}")
    print(f"Unique releases            : {unique:,}")
    print(f"Eventually completed       : {eventual:,}")
    print(f"Failed only                : {failed_only:,}")
    print(f"Recovered after failure    : {recovered:,}")
    print(f"Eventual success rate      : {pct(eventual, unique)}")


def print_prowlarr_log_delta(info) -> None:
    c = info["counts"]
    print("\nNEW PROWLARR LOG ACTIVITY")
    print("-" * 58)
    if info["reset"]:
        print("Log rotation/truncation    : detected")
    if "segments_found" in info:
        print(f"Segments found             : {int(info.get('segments_found', 0)):,}")
        print(f"Segments read this run     : {int(info.get('segments_read', 0)):,}")
        print(f"New segments               : {int(info.get('new_segments', 0)):,}")
    print(f"New log lines              : {c['lines']:,}")
    print(f"NZB indexer search calls   : {c['searches']:,}")
    print(f"Connection failures        : {c['connection_failures']:,}")
    print(f"Invalid releases           : {c['invalid_releases']:,}")
    print(f"Warning lines              : {c['warnings']:,}")
    if info["per_indexer_search"]:
        print("Search calls by indexer:")
        for name, count in info["per_indexer_search"].most_common():
            print(f"  {name:<24} {count:>8,}")


def print_history_import(inserted: int, types: Counter) -> None:
    print("\nPROWLARR API HISTORY")
    print("-" * 58)
    print(f"History rows refreshed     : {inserted:,}")
    if types:
        print("Retained history event types:")
        for k, v in types.most_common(12):
            print(f"  {k:<28} {v:>8,}")



def iso_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


EPISODE_RE = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,2})E(\d{1,3})(?!\d)")


def episode_identity(title: str) -> tuple[str, int, int] | None:
    text = str(title).strip()
    m = EPISODE_RE.search(text)
    if not m:
        return None
    series = text[:m.start()]
    series = re.sub(r"[\s._-]+", " ", series)
    series = re.sub(r"[()\[\]{}]+", " ", series)
    series = re.sub(r"\b(?:19|20)\d{2}\b", " ", series)
    series = re.sub(r"[^A-Za-z0-9 ]+", " ", series).lower()
    series = " ".join(series.split())
    if len(series) < 2:
        return None
    return series, int(m.group(1)), int(m.group(2))


def build_episode_grab_index(conn) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    grabs: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in prowlarr_grab_rows(conn):
        title = str(r["release_title"] or "")
        ident = episode_identity(title)
        dt = iso_dt(str(r["event_time"]))
        if ident is None or dt is None:
            continue
        grabs[ident].append({
            "dt": dt,
            "event_time": str(r["event_time"]),
            "indexer": str(r["indexer_name"] or r["indexer_id"]),
            "title": title,
        })
    for rows in grabs.values():
        rows.sort(key=lambda x: x["dt"])
    return grabs


def latest_attributed_grab(exact_index, episode_index, job_name, outcome_time, max_hours):
    exact = latest_preceding_grab(exact_index, job_name, outcome_time, max_hours)
    if exact is not None:
        return exact, "exact"
    ident = episode_identity(job_name)
    when = iso_dt(outcome_time)
    if ident is None or when is None:
        return None, "unattributed"
    best = None
    max_age = timedelta(hours=max_hours)
    for grab in episode_index.get(ident, []):
        if grab["dt"] > when:
            break
        age = when - grab["dt"]
        if timedelta(0) <= age <= max_age:
            best = grab
    return (best, "episode") if best is not None else (None, "unattributed")


def build_grab_index(conn) -> dict[str, list[dict[str, Any]]]:
    """Index Prowlarr grab events by normalized title, oldest to newest."""
    grabs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in prowlarr_grab_rows(conn):
        title = str(r["release_title"] or "")
        key = normalized_match_title(title)
        dt = iso_dt(str(r["event_time"]))
        if not key or dt is None:
            continue
        grabs[key].append(
            {
                "dt": dt,
                "event_time": str(r["event_time"]),
                "indexer": str(r["indexer_name"] or r["indexer_id"]),
                "title": title,
            }
        )
    for rows in grabs.values():
        rows.sort(key=lambda x: x["dt"])
    return grabs


def latest_preceding_grab(
    grab_index: dict[str, list[dict[str, Any]]],
    job_name: str,
    outcome_time: str,
    max_hours: int,
) -> dict[str, Any] | None:
    key = normalized_match_title(job_name)
    when = iso_dt(outcome_time)
    if not key or when is None:
        return None

    best = None
    max_age = timedelta(hours=max_hours)

    for grab in grab_index.get(key, []):
        gdt = grab["dt"]
        if gdt > when:
            break
        age = when - gdt
        if timedelta(0) <= age <= max_age:
            best = grab
    return best


def attributed_attempts(conn, max_hours: int):
    exact_grabs = build_grab_index(conn)
    episode_grabs = build_episode_grab_index(conn)
    result = defaultdict(lambda: {
        "attempts": 0, "completed": 0, "failed": 0, "repair": 0, "heavy": 0,
        "exact": 0, "episode": 0,
    })
    confidence = {"exact": 0, "episode": 0, "unattributed": 0}
    rows = conn.execute("""
        SELECT event_time, job_name, status, max_repair_blocks
        FROM jobs ORDER BY event_time
    """).fetchall()
    for r in rows:
        grab, method = latest_attributed_grab(
            exact_grabs, episode_grabs, str(r["job_name"]), str(r["event_time"]), max_hours
        )
        confidence[method] += 1
        if grab is None:
            continue
        d = result[grab["indexer"]]
        d["attempts"] += 1
        d[method] += 1
        status = str(r["status"]).lower()
        d["completed"] += status == "completed"
        d["failed"] += status == "failed"
        blocks = int(r["max_repair_blocks"] or 0)
        d["repair"] += blocks > 0
        d["heavy"] += blocks >= 10
    return dict(result), confidence


def attributed_unique_releases(conn, max_hours: int):
    exact_grabs = build_grab_index(conn)
    episode_grabs = build_episode_grab_index(conn)
    grouped = defaultdict(list)
    rows = conn.execute("""
        SELECT event_time, job_name, status, max_repair_blocks
        FROM jobs ORDER BY event_time
    """).fetchall()
    for r in rows:
        grouped[normalized_release_identity(str(r["job_name"]))].append(r)

    result = defaultdict(lambda: {
        "releases": 0, "completed": 0, "failed": 0, "repair": 0, "heavy": 0,
        "recovered": 0, "exact": 0, "episode": 0,
    })
    confidence = {"exact": 0, "episode": 0, "unattributed": 0}
    unattributed = []

    for attempts in grouped.values():
        final = attempts[-1]
        grab, method = latest_attributed_grab(
            exact_grabs, episode_grabs, str(final["job_name"]), str(final["event_time"]), max_hours
        )
        confidence[method] += 1
        if grab is None:
            unattributed.append({
                "event_time": str(final["event_time"]),
                "job_name": str(final["job_name"]),
                "status": str(final["status"]),
                "repair_blocks": max(int(a["max_repair_blocks"] or 0) for a in attempts),
            })
            continue
        d = result[grab["indexer"]]
        d["releases"] += 1
        d[method] += 1
        final_status = str(final["status"]).lower()
        d["completed"] += final_status == "completed"
        d["failed"] += final_status == "failed"
        blocks = max(int(a["max_repair_blocks"] or 0) for a in attempts)
        d["repair"] += blocks > 0
        d["heavy"] += blocks >= 10
        failed_seen = False
        recovered = False
        for a in attempts:
            status = str(a["status"]).lower()
            if status == "failed":
                failed_seen = True
            elif status == "completed" and failed_seen:
                recovered = True
                break
        d["recovered"] += recovered
    unattributed.sort(key=lambda x: x["event_time"], reverse=True)
    return dict(result), confidence, unattributed


def print_attributed_outcomes(conn, max_hours: int) -> None:
    attempt_rows, attempt_conf = attributed_attempts(conn, max_hours)
    release_rows, release_conf, unattributed = attributed_unique_releases(conn, max_hours)

    print("\nINDEXER -> SAB ATTRIBUTED ATTEMPTS")
    print("-" * 112)
    print(f"Matching: exact title first; episode fallback second; <= {max_hours} hours old")
    print(f"{'Indexer':<22}{'Attempts':>10}{'Completed':>11}{'Failed':>9}{'Success':>10}{'Repair':>9}{'Heavy':>9}{'Exact':>9}{'Episode':>10}")
    print("-" * 112)
    for name, d in sorted(attempt_rows.items()):
        print(f"{name[:21]:<22}{d['attempts']:>10,}{d['completed']:>11,}{d['failed']:>9,}{pct(d['completed'],d['attempts']):>10}{d['repair']:>9,}{d['heavy']:>9,}{d['exact']:>9,}{d['episode']:>10,}")
    print(f"Attribution confidence     : exact {attempt_conf['exact']:,}, episode fallback {attempt_conf['episode']:,}, unattributed {attempt_conf['unattributed']:,}")

    print("\nINDEXER -> FINAL UNIQUE RELEASE OUTCOME")
    print("-" * 124)
    print(f"{'Indexer':<22}{'Releases':>10}{'Completed':>11}{'Failed':>9}{'Success':>10}{'Recovered':>11}{'Repair':>9}{'Heavy':>9}{'Exact':>9}{'Episode':>10}")
    print("-" * 124)
    for name, d in sorted(release_rows.items()):
        print(f"{name[:21]:<22}{d['releases']:>10,}{d['completed']:>11,}{d['failed']:>9,}{pct(d['completed'],d['releases']):>10}{d['recovered']:>11,}{d['repair']:>9,}{d['heavy']:>9,}{d['exact']:>9,}{d['episode']:>10,}")
    print(f"Attribution confidence     : exact {release_conf['exact']:,}, episode fallback {release_conf['episode']:,}, unattributed {release_conf['unattributed']:,}")

    if unattributed:
        print("\nUNATTRIBUTED UNIQUE RELEASES")
        print("-" * 110)
        for row in unattributed[:20]:
            ident = episode_identity(row["job_name"])
            ep = f" [{ident[0]} S{ident[1]:02d}E{ident[2]:02d}]" if ident else ""
            print(f"- {row['event_time']}  {row['status']:<9} {row['job_name']}{ep}")


def print_correlation(conn) -> None:
    rows = correlate_grabs(conn)
    print("\nPROWLARR RAW GRAB-EVENT MATCHING (LOW-PRIORITY DIAGNOSTIC)")
    print("-" * 100)
    if not rows:
        print(
            "No matchable grab records yet. This usually means either Prowlarr "
            "history has no grab titles in the retained window, or its eventType "
            "serialization needs one diagnostic pass."
        )
        return
    print(
        f"{'Indexer':<22}{'Grab titles':>11}{'Matched':>9}{'Match %':>9}"
        f"{'Completed':>11}{'Failed':>9}{'Success':>9}{'Repair':>9}{'Heavy':>8}"
    )
    print("-" * 100)
    for name, d in sorted(rows.items()):
        resolved = d['eventually_completed'] + d['failed_only']
        print(
            f"{name[:21]:<22}{d['grabs_with_title']:>11,}{d['matched']:>9,}"
            f"{pct(d['matched'], d['grabs_with_title']):>9}"
            f"{d['eventually_completed']:>11,}{d['failed_only']:>9,}"
            f"{pct(d['eventually_completed'], resolved):>9}"
            f"{d['repair']:>9,}{d['heavy_repair']:>8,}"
        )


def print_prowlarr_diagnostics(conn) -> None:
    print("\nPROWLARR STORED DIAGNOSTICS")
    print("-" * 74)
    rows = conn.execute(
        """
        SELECT COALESCE(indexer_name,'(unknown)') indexer_name, kind, COUNT(*) n
        FROM prowlarr_log_errors
        GROUP BY COALESCE(indexer_name,'(unknown)'), kind
        ORDER BY n DESC
        """
    ).fetchall()
    if not rows:
        print("No stored Prowlarr indexer diagnostics.")
        return
    for r in rows:
        print(f"{r['indexer_name']:<26} {r['kind']:<24} {int(r['n']):>8,}")


# ===========================================================================
# CLI / main
# ===========================================================================


def _historical_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Compact before/after counters used by the destructive-looking but additive replay."""
    queries = {
        "sab_events": "SELECT COUNT(*) FROM sab_log_events",
        "arr_events": "SELECT COUNT(*) FROM arr_events",
        "arr_searches": "SELECT COUNT(*) FROM arr_events WHERE event_type='search_started'",
        "arr_candidates": "SELECT COUNT(*) FROM arr_search_candidates",
        "prowlarr_searches": "SELECT COUNT(*) FROM prowlarr_log_searches",
        "prowlarr_searches_with_counts": "SELECT COUNT(*) FROM prowlarr_log_searches WHERE result_count IS NOT NULL",
        "target_known": "SELECT COUNT(*) FROM arr_search_targets",
    }
    out: dict[str, int] = {}
    for key, sql in queries.items():
        try:
            out[key] = int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            out[key] = 0
    return out


def _consistent_sqlite_backup(db_path: Path) -> Path:
    """Create a WAL-safe SQLite backup next to the live DB.

    sqlite3.Connection.backup() copies the logical database, including committed WAL
    content, instead of blindly copying only the .db file while -wal/-shm may exist.
    """
    stamp = local_now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = db_path.with_name(f"{db_path.stem}.pre_v{VERSION.replace(chr(46), chr(95))}_maintenance_{stamp}{db_path.suffix or '.db'}")
    src = sqlite3.connect(str(db_path), timeout=30.0)
    dst = sqlite3.connect(str(backup_path), timeout=30.0)
    try:
        src.backup(dst)
        dst.commit()
        check = dst.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise sqlite3.DatabaseError(f"backup quick_check failed: {check!r}")
    finally:
        dst.close()
        src.close()
    return backup_path


def _rewind_log_cursors(conn: sqlite3.Connection) -> None:
    """Rewind only parser cursors; never erase historical business data."""
    conn.execute("UPDATE sab_log_segments SET byte_offset=0,file_size=0")
    conn.execute("UPDATE prowlarr_log_segments SET byte_offset=0,file_size=0")
    conn.execute("UPDATE arr_log_segments SET byte_offset=0,file_size=0")
    # v7.10.2 had a one-shot evidence replay marker. Full replay supersedes it;
    # removing the marker lets a later normal run use the migration only if needed.
    conn.execute("DELETE FROM collector_migrations WHERE name='raw_search_evidence_v2'")


def _known_indexer_names_from_db(conn: sqlite3.Connection) -> list[str]:
    names: set[str] = set()
    for sql in (
        "SELECT name FROM prowlarr_indexers WHERE COALESCE(name,'')<>''",
        "SELECT DISTINCT indexer_name AS name FROM prowlarr_history WHERE COALESCE(indexer_name,'')<>''",
        "SELECT DISTINCT indexer_name AS name FROM prowlarr_log_searches WHERE COALESCE(indexer_name,'')<>''",
        "SELECT DISTINCT indexer AS name FROM arr_search_candidates WHERE COALESCE(indexer,'')<>''",
    ):
        try:
            for row in conn.execute(sql):
                name = str(row[0] or "").strip()
                if name:
                    names.add(name)
        except sqlite3.Error:
            pass
    return sorted(names, key=str.lower)


def run_full_historical_backfill(
    *, db_path: Path, sab_log: Path, prow_log: Path,
    sonarr_log: Path | None, radarr_log: Path | None,
) -> int:
    """Replay all retained logs into the existing DB without deleting old history.

    Existing deterministic rows are updated/de-duplicated; newly recoverable evidence
    is inserted. Data older than the retained rotations is preserved untouched.
    """
    if not db_path.is_file():
        print(f"ERROR: database does not exist for backfill: {db_path}", file=sys.stderr)
        return 2

    print("\nFULL HISTORICAL BACKFILL")
    print("-" * 78)
    print("Mode      : additive/in-place replay (no historical tables are wiped)")
    print("Database  :", db_path)
    print("Creating consistent SQLite backup before changing cursor state...")
    backup_path = _consistent_sqlite_backup(db_path)
    print("Backup    :", backup_path)

    conn = open_db(db_path)
    try:
        before = _historical_counts(conn)
        known_indexers = _known_indexer_names_from_db(conn)
        if not known_indexers:
            raise RuntimeError(
                "No known Prowlarr indexer names exist in the database. Run one normal collector "
                "cycle first so the Usenet indexer list can be stored, then retry --backfill-all."
            )
        print("Indexers  :", ", ".join(known_indexers))

        with conn:
            _rewind_log_cursors(conn)

        # Commit cursor rewind before parser calls. Each parser is deterministic and
        # leaves its segment offsets at EOF, so normal incremental collection resumes
        # immediately after this command completes.
        with conn:
            sab_delta = parse_sab_log_family(conn, sab_log)
            cancel_scan = reconcile_retained_sab_cancellations(conn, sab_log)
            history_cancels = reconcile_sab_history_cancellations(conn)
            diagnostics = rebuild_sab_attempt_diagnostics(conn)

            prow_delta = parse_prowlarr_log_family(conn, prow_log, known_indexers)
            sonarr_delta = parse_arr_log_family(conn, sonarr_log, "sonarr") if sonarr_log is not None else None
            radarr_delta = parse_arr_log_family(conn, radarr_log, "radarr") if radarr_log is not None else None
            reclassify_arr_search_origins(conn)

            conn.execute(
                "INSERT INTO collector_runs(collected_at,version,script_path,db_path,source_errors_json) VALUES(?,?,?,?,?)",
                (now_iso(), VERSION, str(Path(__file__).resolve()), str(db_path.resolve()), json.dumps({})),
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_migrations(name,applied_at,details) VALUES(?,?,?)",
                (
                    "full_historical_replay_v7_10_2", now_iso(),
                    json.dumps({
                        "backup": str(backup_path),
                        "sab_lines": int(sab_delta.get("lines", 0)),
                        "prowlarr_lines": int((prow_delta.get("counts") or {}).get("lines", 0)),
                        "sonarr_lines": int((sonarr_delta or {}).get("lines", 0)),
                        "radarr_lines": int((radarr_delta or {}).get("lines", 0)),
                    }, sort_keys=True),
                ),
            )

        after = _historical_counts(conn)
        unresolved = max(0, after["arr_searches"] - after["target_known"])
        print("\nReplay complete")
        print("-" * 78)
        print(f"SAB lines replayed         : {int(sab_delta.get('lines', 0)):,}")
        print(f"Prowlarr lines replayed    : {int((prow_delta.get('counts') or {}).get('lines', 0)):,}")
        print(f"Sonarr lines replayed      : {int((sonarr_delta or {}).get('lines', 0)):,}")
        print(f"Radarr lines replayed      : {int((radarr_delta or {}).get('lines', 0)):,}")
        print(f"SAB diagnostics rebuilt    : {int(diagnostics):,}")
        print(f"Cancellation scan          : {int(cancel_scan.get('accepted', 0)):,} accepted; {int(history_cancels.get('events', 0)):,} history events")
        print("\nDatabase evidence")
        print("-" * 78)
        labels = (
            ("SAB lifecycle events", "sab_events"),
            ("Arr orchestration events", "arr_events"),
            ("Arr logical searches", "arr_searches"),
            ("Raw Arr candidates", "arr_candidates"),
            ("Prowlarr query events", "prowlarr_searches"),
            ("Prowlarr queries with result count", "prowlarr_searches_with_counts"),
            ("Exact search-time targets", "target_known"),
        )
        for label, key in labels:
            print(f"{label:<34}: {before[key]:>8,} -> {after[key]:>8,}  ({after[key]-before[key]:+d})")
        print(f"Historical target unresolved      : {unresolved:,}")
        print("\nNOTE: target-quality snapshots cannot be recreated for dates where no historical")
        print("profile snapshot or equally strong retained evidence exists. Raw query/result")
        print("evidence is still backfilled, and zero-result indexer queries are retained.")
        print("\nIncremental cursors now point to EOF; future normal collector runs remain incremental.")
        return 0
    except Exception as exc:
        print(f"\nERROR during historical backfill: {exc}", file=sys.stderr)
        print(f"The pre-backfill backup is preserved at: {backup_path}", file=sys.stderr)
        return 1
    finally:
        conn.close()

def _rewind_search_cursors(conn: sqlite3.Connection) -> None:
    """Rewind only Prowlarr/Arr parsers for a derived search-layer rebuild."""
    conn.execute("UPDATE prowlarr_log_segments SET byte_offset=0,file_size=0")
    conn.execute("UPDATE arr_log_segments SET byte_offset=0,file_size=0")
    conn.execute("DELETE FROM collector_migrations WHERE name='raw_search_evidence_v2'")


def run_search_layer_rebuild(
    *, db_path: Path, prow_log: Path, sonarr_log: Path | None, radarr_log: Path | None,
) -> int:
    """Rebuild only derived Arr/Prowlarr search evidence from retained logs.

    SAB jobs/history/provider data and the broader Arr orchestration history are kept.
    Exact API profile snapshots are preserved. Raw candidates and Prowlarr query rows
    are regenerated from byte 0 with the current parser, then historical target
    evidence is reconstructed from Arr's per-release decision stream.
    """
    if not db_path.is_file():
        print(f"ERROR: database does not exist for search rebuild: {db_path}", file=sys.stderr)
        return 2

    print("\nDERIVED SEARCH-LAYER REBUILD")
    print("-" * 78)
    print("Mode      : rebuild raw search/query/target evidence only")
    print("Database  :", db_path)
    print("Preserved : SAB lifecycle/history/providers + Arr orchestration + exact API targets")
    print("Creating consistent SQLite backup before rebuilding derived search data...")
    backup_path = _consistent_sqlite_backup(db_path)
    print("Backup    :", backup_path)

    conn = open_db(db_path)
    try:
        before = _historical_counts(conn)
        known_indexers = _known_indexer_names_from_db(conn)
        if not known_indexers:
            raise RuntimeError("No known Prowlarr indexer names exist in the database.")
        print("Indexers  :", ", ".join(known_indexers))

        with conn:
            exact_targets = int(conn.execute(
                "SELECT COUNT(*) FROM arr_search_targets WHERE source='arr_api_snapshot'"
            ).fetchone()[0] or 0)
            old_inferred = int(conn.execute(
                "SELECT COUNT(*) FROM arr_search_targets WHERE source<>'arr_api_snapshot'"
            ).fetchone()[0] or 0)
            old_candidates = int(conn.execute("SELECT COUNT(*) FROM arr_search_candidates").fetchone()[0] or 0)
            old_queries = int(conn.execute("SELECT COUNT(*) FROM prowlarr_log_searches").fetchone()[0] or 0)

            conn.execute("DELETE FROM arr_search_candidates")
            conn.execute("DELETE FROM prowlarr_log_searches")
            conn.execute("DELETE FROM arr_search_targets WHERE source<>'arr_api_snapshot'")
            _rewind_search_cursors(conn)

        with conn:
            prow_delta = parse_prowlarr_log_family(conn, prow_log, known_indexers)
            sonarr_delta = parse_arr_log_family(conn, sonarr_log, "sonarr") if sonarr_log is not None else None
            radarr_delta = parse_arr_log_family(conn, radarr_log, "radarr") if radarr_log is not None else None
            reclassify_arr_search_origins(conn)
            invalid_api_targets = revalidate_arr_api_search_targets(conn)
            inferred = infer_historical_arr_search_targets(conn)

            conn.execute(
                "INSERT INTO collector_runs(collected_at,version,script_path,db_path,source_errors_json) VALUES(?,?,?,?,?)",
                (now_iso(), VERSION, str(Path(__file__).resolve()), str(db_path.resolve()), json.dumps({})),
            )
            conn.execute(
                "INSERT OR REPLACE INTO collector_migrations(name,applied_at,details) VALUES(?,?,?)",
                (
                    "derived_search_rebuild_v7_10_9", now_iso(),
                    json.dumps({
                        "backup": str(backup_path),
                        "old_candidates": old_candidates,
                        "old_queries": old_queries,
                        "preserved_exact_targets_before_validation": exact_targets,
                        "invalidated_conflicting_api_targets": len(invalid_api_targets),
                        "removed_old_inferred_targets": old_inferred,
                        "prowlarr_lines": int((prow_delta.get("counts") or {}).get("lines", 0)),
                        "sonarr_lines": int((sonarr_delta or {}).get("lines", 0)),
                        "radarr_lines": int((radarr_delta or {}).get("lines", 0)),
                        "historical_targets": inferred,
                    }, sort_keys=True),
                ),
            )

        after = _historical_counts(conn)
        historical_targets = int(conn.execute(
            "SELECT COUNT(*) FROM arr_search_targets WHERE source='historical_arr_decision'"
        ).fetchone()[0] or 0)
        exact_targets_after = int(conn.execute(
            "SELECT COUNT(*) FROM arr_search_targets WHERE source='arr_api_snapshot'"
        ).fetchone()[0] or 0)
        unresolved = max(0, after["arr_searches"] - after["target_known"])
        accepted_candidates = int(conn.execute(
            "SELECT COUNT(*) FROM arr_search_candidates WHERE decision='accepted'"
        ).fetchone()[0] or 0)
        profile_rejected_candidates = int(conn.execute(
            "SELECT COUNT(*) FROM arr_search_candidates WHERE lower(COALESCE(rejection_reasons,'')) LIKE '%not wanted in profile%'"
        ).fetchone()[0] or 0)

        print("\nSearch-layer rebuild complete")
        print("-" * 78)
        print(f"Prowlarr lines replayed           : {int((prow_delta.get('counts') or {}).get('lines', 0)):,}")
        print(f"Sonarr lines replayed             : {int((sonarr_delta or {}).get('lines', 0)):,}")
        print(f"Radarr lines replayed             : {int((radarr_delta or {}).get('lines', 0)):,}")
        print("\nDerived evidence")
        print("-" * 78)
        print(f"Raw Arr candidates                : {old_candidates:>8,} -> {after['arr_candidates']:>8,}")
        print(f"Conflicting API targets invalidated: {len(invalid_api_targets):>8,}")
        print(f"Prowlarr query events             : {old_queries:>8,} -> {after['prowlarr_searches']:>8,}")
        print(f"Queries with result count         : {after['prowlarr_searches_with_counts']:>8,}")
        print(f"Explicit accepted candidates      : {accepted_candidates:>8,}")
        print(f"Profile-rejected candidates       : {profile_rejected_candidates:>8,}")
        print(f"Exact API targets preserved       : {exact_targets_after:>8,}")
        print(f"Historical targets reconstructed  : {historical_targets:>8,}")
        print(f"  high confidence                 : {int(inferred.get('high', 0)):>8,}")
        print(f"  medium confidence               : {int(inferred.get('medium', 0)):>8,}")
        print(f"Historical target unresolved      : {unresolved:>8,}")
        print("\nHistorical target semantics:")
        print("  A quality counts when a correct-media candidate passed the Arr quality-profile")
        print("  check, even if Arr later rejected it for Custom Format score, size, language,")
        print("  retention or another downstream rule. Explicit 'not wanted in profile' qualities")
        print("  are excluded. This reconstructs observed profile-compatible qualities, not a")
        print("  profile name/cutoff that was never retained.")
        print("\nIncremental Prowlarr/Arr cursors now point to EOF; normal collection can resume.")
        return 0
    except Exception as exc:
        print(f"\nERROR during search-layer rebuild: {exc}", file=sys.stderr)
        print(f"The pre-rebuild backup is preserved at: {backup_path}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    script_db = str(database_path(config_path()))
    p = argparse.ArgumentParser(
        description="Persistent SABnzbd + Prowlarr + Sonarr/Radarr NZB monitor."
    )
    p.add_argument("--url", default=os.environ.get("SABNZBD_URL", "http://localhost:8085"))
    p.add_argument("--api-key", default=os.environ.get("SABNZBD_API_KEY"))
    p.add_argument("--log", default=os.environ.get("SABNZBD_LOG"), help="SAB active log or log directory; rotated siblings are auto-discovered")
    p.add_argument("--log-dir", default=os.environ.get("SABNZBD_LOG_DIR"), help="Directory containing sabnzbd.log, sabnzbd.log.1, .2, ...")
    p.add_argument("--db", default=os.environ.get("SABNZBD_DB", script_db))

    p.add_argument(
        "--prowlarr-url",
        default=os.environ.get("PROWLARR_URL", "http://localhost:9696"),
    )
    p.add_argument(
        "--prowlarr-api-key",
        default=os.environ.get("PROWLARR_API_KEY"),
    )
    p.add_argument(
        "--prowlarr-log",
        default=os.environ.get("PROWLARR_LOG"),
    )
    p.add_argument(
        "--prowlarr-log-dir",
        default=os.environ.get("PROWLARR_LOG_DIR"),
        help="Directory containing prowlarr.txt, prowlarr.0.txt, .1.txt, ...",
    )
    p.add_argument(
        "--sonarr-log",
        default=os.environ.get("SONARR_LOG"),
        help="Sonarr active log or log directory; numbered rotations are auto-discovered",
    )
    p.add_argument(
        "--sonarr-log-dir",
        default=os.environ.get("SONARR_LOG_DIR"),
        help="Directory containing sonarr.txt, sonarr.0.txt, .1.txt, ...",
    )
    p.add_argument(
        "--radarr-log",
        default=os.environ.get("RADARR_LOG"),
        help="Radarr active log or log directory; numbered rotations are auto-discovered",
    )
    p.add_argument(
        "--radarr-log-dir",
        default=os.environ.get("RADARR_LOG_DIR"),
        help="Directory containing radarr.txt, radarr.0.txt, .1.txt, ...",
    )
    p.add_argument(
        "--sonarr-url",
        default=os.environ.get("SONARR_URL", "http://localhost:8989"),
        help="Optional Sonarr base URL used to snapshot the active quality-profile target for new searches",
    )
    p.add_argument(
        "--sonarr-api-key",
        default=os.environ.get("SONARR_API_KEY"),
        help="Optional Sonarr API key. Without it raw candidates are retained but Sonarr target-quality scoring stays unresolved.",
    )
    p.add_argument(
        "--radarr-url",
        default=os.environ.get("RADARR_URL", "http://localhost:7878"),
        help="Optional Radarr base URL used to snapshot the active quality-profile target for new searches",
    )
    p.add_argument(
        "--radarr-api-key",
        default=os.environ.get("RADARR_API_KEY"),
        help="Optional Radarr API key. Without it raw candidates are retained but Radarr target-quality scoring stays unresolved.",
    )
    p.add_argument(
        "--match-window-hours",
        type=int,
        default=168,
        help="Maximum age of a preceding Prowlarr grab used to attribute a SAB outcome (default: 168 = 7 days)",
    )
    p.add_argument(
        "--debug-prowlarr",
        action="store_true",
        help="Print compact raw Prowlarr API shape diagnostics if stats/history need tuning.",
    )
    p.add_argument(
        "--backfill-all",
        action="store_true",
        help=(
            "One-time full historical log replay. Creates a consistent SQLite backup, "
            "rewinds SAB/Prowlarr/Sonarr/Radarr log cursors to byte 0, reparses every "
            "retained rotation with the current parser, then leaves cursors at EOF."
        ),
    )
    p.add_argument(
        "--rebuild-search-layer",
        action="store_true",
        help=(
            "One-time derived search-layer rebuild. Backs up the DB, clears only raw Arr "
            "candidate/Prowlarr query/inferred-target rows, replays retained Prowlarr/Arr "
            "logs from byte 0, reconstructs historical profile-compatible qualities, and "
            "preserves SAB/history/provider data and exact API target snapshots."
        ),
    )
    return p.parse_args()


def _main(args: argparse.Namespace) -> int:

    required = [
        ("--log/--log-dir", args.log or args.log_dir),
        ("--prowlarr-log/--prowlarr-log-dir", args.prowlarr_log or args.prowlarr_log_dir),
    ]
    if not args.backfill_all and not args.rebuild_search_layer:
        required.extend([
            ("--api-key", args.api_key),
            ("--prowlarr-api-key", args.prowlarr_api_key),
        ])
    missing = [name for name, value in required if not value or ("api-key" in name and placeholder(value))]
    if missing:
        print("ERROR: missing " + ", ".join(missing), file=sys.stderr)
        return 2

    sab_log = Path(args.log_dir or args.log)
    prow_log = Path(args.prowlarr_log_dir or args.prowlarr_log)
    sonarr_log = Path(args.sonarr_log_dir or args.sonarr_log) if (args.sonarr_log_dir or args.sonarr_log) else None
    radarr_log = Path(args.radarr_log_dir or args.radarr_log) if (args.radarr_log_dir or args.radarr_log) else None
    db_path = Path(args.db)
    sab_family = discover_sab_logs(sab_log)
    prow_family = discover_numbered_text_logs(prow_log, "prowlarr", include_debug=True)
    if not sab_family:
        print(f"ERROR: SAB log family not found: {sab_log}", file=sys.stderr)
        return 2
    if not prow_family:
        print(f"ERROR: Prowlarr log family not found: {prow_log}", file=sys.stderr)
        return 2
    if sonarr_log is not None and not discover_numbered_text_logs(sonarr_log, "sonarr", include_debug=True):
        print(f"WARNING: Sonarr log family not found: {sonarr_log}", file=sys.stderr)
        sonarr_log = None
    if radarr_log is not None and not discover_numbered_text_logs(radarr_log, "radarr", include_debug=True):
        print(f"WARNING: Radarr log family not found: {radarr_log}", file=sys.stderr)
        radarr_log = None

    if args.rebuild_search_layer:
        return run_search_layer_rebuild(
            db_path=db_path, prow_log=prow_log,
            sonarr_log=sonarr_log, radarr_log=radarr_log,
        )
    if args.backfill_all:
        return run_full_historical_backfill(
            db_path=db_path, sab_log=sab_log, prow_log=prow_log,
            sonarr_log=sonarr_log, radarr_log=radarr_log,
        )

    print("=" * 78)
    print(f"SABnzbd + Prowlarr + Arr Persistent Monitor v{VERSION}")
    print("=" * 78)
    print(f"SAB      : {args.url}  (v{sab_version(args.url)})")
    print(f"Prowlarr : {args.prowlarr_url}  (v{prowlarr_version(args.prowlarr_url, args.prowlarr_api_key)})")
    print(f"SAB logs : {sab_log}")
    print(f"Segments : {len(sab_family)} ({sab_family[0].name} -> {sab_family[-1].name})")
    print(f"Prowl logs: {prow_log}")
    print(f"Prowl segs: {len(prow_family)} ({prow_family[0].name} -> {prow_family[-1].name})")
    print(f"Sonarr   : {sonarr_log if sonarr_log is not None else 'disabled'}")
    print(f"Radarr   : {radarr_log if radarr_log is not None else 'disabled'}")
    print(f"Sonarr API target snapshot: {'configured' if args.sonarr_api_key else 'not configured'}")
    print(f"Radarr API target snapshot: {'configured' if args.radarr_api_key else 'not configured'}")
    print(f"Database : {db_path}")

    source_errors: dict[str, str] = {}
    def fetch_source(label, loader, fallback):
        try:
            return loader()
        except Exception as exc:
            source_errors[label] = redact(str(exc), [args.api_key, args.prowlarr_api_key,
                                                     args.sonarr_api_key, args.radarr_api_key])
            print(f"WARNING: {label}: {source_errors[label]}", file=sys.stderr)
            return fallback

    sab_stats_raw = fetch_source("SABnzbd provider API", lambda: sab_api_get(args.url, args.api_key, "server_stats"), None)
    sab_config_raw = fetch_source("SABnzbd server metadata", lambda: sab_api_get(args.url, args.api_key, "get_config", section="servers"), None)
    server_config = sab_server_config_map(sab_config_raw)
    providers = sab_provider_report(sab_stats_raw, server_config) if sab_stats_raw is not None else []
    sab_history_rows = fetch_source("SABnzbd history API", lambda: fetch_sab_history(args.url, args.api_key), [])
    indexers = fetch_source("Prowlarr indexer API", lambda: load_usenet_indexers(args.prowlarr_url, args.prowlarr_api_key), {})
    prow_stats_raw = fetch_source("Prowlarr statistics API", lambda: prowlarr_api_get(args.prowlarr_url, args.prowlarr_api_key, "/api/v1/indexerstats"), None)
    prow_history_raw = fetch_source("Prowlarr history API", lambda: fetch_prowlarr_history(args.prowlarr_url, args.prowlarr_api_key), [])

    arr_snapshots: dict[str, dict[str, Any]] = {}
    arr_snapshot_errors: dict[str, str] = {}
    for app_name, base_url, api_key in (
        ("sonarr", args.sonarr_url, args.sonarr_api_key),
        ("radarr", args.radarr_url, args.radarr_api_key),
    ):
        if not api_key:
            arr_snapshots[app_name] = {
                "app": app_name, "configured": False, "captured_at": now_iso(), "profiles": {}, "entities": {}
            }
            continue
        try:
            arr_snapshots[app_name] = fetch_arr_profile_snapshot(app_name, base_url, api_key)
        except Exception as exc:
            arr_snapshots[app_name] = {
                "app": app_name, "configured": True, "captured_at": now_iso(), "profiles": {}, "entities": {}
            }
            arr_snapshot_errors[app_name] = redact(str(exc), [args.sonarr_api_key, args.radarr_api_key])
            source_errors[f"{app_name.title()} profile API"] = arr_snapshot_errors[app_name]

    try:
        conn = open_db(db_path)
        if "Prowlarr indexer API" in source_errors:
            indexers = {int(r["indexer_id"]): {"name": r["name"], "protocol": r["protocol"]}
                        for r in conn.execute("SELECT * FROM prowlarr_indexers WHERE lower(protocol) IN ('usenet','nzb')")}
        with conn:
            collected = now_iso()
            prev_row = conn.execute("SELECT MAX(collected_at) AS ts FROM collector_runs").fetchone()
            previous_collected = str(prev_row["ts"] or "") if prev_row is not None else ""
            collected_dt = datetime.fromisoformat(collected)
            if previous_collected:
                try:
                    prev_dt = datetime.fromisoformat(previous_collected)
                    fresh_dt = max(prev_dt - timedelta(minutes=5), collected_dt - timedelta(hours=6))
                except Exception:
                    fresh_dt = collected_dt - timedelta(hours=2)
            else:
                fresh_dt = collected_dt - timedelta(hours=2)
            fresh_after = fresh_dt.isoformat(timespec="milliseconds")
            save_provider_snapshots(conn, collected, providers)
            # Refresh SAB history before parsing logs so history/archive UUID actions
            # can be resolved back to release names when possible.
            sab_history_imported = import_sab_history(conn, sab_history_rows)
            sab_delta = parse_sab_log_family(conn, sab_log)
            cancel_backfill = reconcile_retained_sab_cancellations(conn, sab_log)
            history_cancels = reconcile_sab_history_cancellations(conn)
            reconciled_jobs = int(cancel_backfill.get("jobs", 0)) + int(history_cancels.get("jobs", 0))
            reconciled_events = int(cancel_backfill.get("events", 0)) + int(history_cancels.get("events", 0))
            if reconciled_jobs or reconciled_events:
                sab_delta["backfill_cancelled"] = int(sab_delta.get("backfill_cancelled", 0)) + reconciled_jobs
                sab_delta["cancelled"] = int(sab_delta.get("cancelled", 0)) + reconciled_jobs
                sab_delta["backfill_lifecycle_events"] = int(sab_delta.get("backfill_lifecycle_events", 0)) + reconciled_events
                sab_delta["events"] = int(sab_delta.get("events", 0)) + reconciled_events
                conn.execute(
                    """UPDATE sab_log_family_runs SET cancelled=cancelled+?, backfill_cancelled=backfill_cancelled+?,
                       lifecycle_events=lifecycle_events+?, backfill_lifecycle_events=backfill_lifecycle_events+?
                       WHERE id=(SELECT MAX(id) FROM sab_log_family_runs)""",
                    (reconciled_jobs, reconciled_jobs, reconciled_events, reconciled_events),
                )
            diagnostics_rebuilt = rebuild_sab_attempt_diagnostics(conn)

            if "Prowlarr indexer API" not in source_errors:
                save_prowlarr_indexers(conn, indexers)
            stat_rows = save_prowlarr_stats(conn, collected, indexers, prow_stats_raw) if prow_stats_raw is not None else []
            imported_history, hist_types = import_prowlarr_history(
                conn, indexers, prow_history_raw
            )
            prow_log_delta = parse_prowlarr_log_family(
                conn,
                prow_log,
                [str(v.get("name", k)) for k, v in indexers.items()],
            )
            sonarr_delta = parse_arr_log_family(conn, sonarr_log, "sonarr") if sonarr_log is not None else None
            radarr_delta = parse_arr_log_family(conn, radarr_log, "radarr") if radarr_log is not None else None
            coverage_backfill = backfill_search_coverage_evidence(
                conn, prow_log, [str(v.get("name", k)) for k, v in indexers.items()],
                sonarr_log, radarr_log,
            )
            for app_name in ("sonarr", "radarr"):
                snap = arr_snapshots.get(app_name) or {"app": app_name, "configured": False}
                applied = 0
                error = arr_snapshot_errors.get(app_name)
                if not error:
                    try:
                        applied = capture_arr_search_targets(conn, snap, fresh_after=fresh_after)
                    except Exception as exc:
                        error = str(exc)
                save_arr_profile_snapshot_run(conn, snap, targets_applied=applied, error=error)
            invalid_api_targets = revalidate_arr_api_search_targets(conn)
            # If a mutable live snapshot was rejected for a newly observed search,
            # immediately fall back to that search's retained Arr decision evidence.
            # This keeps future temporary profile-switch tests correct without
            # requiring a maintenance rebuild just to obtain a safe historical target.
            recent_targetless = {
                str(r[0]) for r in conn.execute(
                    """SELECT e.event_id FROM arr_events e
                       LEFT JOIN arr_search_targets t ON t.search_event_id=e.event_id
                       WHERE e.event_type='search_started' AND e.event_time>=?
                         AND t.search_event_id IS NULL""",
                    (fresh_after,),
                ).fetchall()
            }
            infer_ids = set(invalid_api_targets) | recent_targetless
            if infer_ids:
                infer_historical_arr_search_targets(conn, infer_ids)
            reclassify_arr_search_origins(conn)
            conn.execute(
                "INSERT INTO collector_runs(collected_at,version,script_path,db_path,source_errors_json) VALUES(?,?,?,?,?)",
                (now_iso(), VERSION, str(Path(__file__).resolve()), str(db_path.resolve()), json.dumps(source_errors)),
            )

        print_provider(providers)
        print_last_collection(conn, sab_delta, prow_log_delta)
        print("\nSAB HISTORY DIAGNOSTICS")
        print("-" * 58)
        print(f"History rows refreshed     : {sab_history_imported:,}")
        failed_diag = conn.execute("SELECT COUNT(*) n FROM sab_history WHERE COALESCE(fail_message,'')<>''").fetchone()["n"]
        staged_diag = conn.execute("SELECT COUNT(*) n FROM sab_history WHERE COALESCE(stage_log_json,'') NOT IN ('','[]')").fetchone()["n"]
        print(f"Rows with failure reason   : {int(failed_diag):,}")
        print(f"Rows with SAB stage log    : {int(staged_diag):,}")
        print(f"Rotated log segments found: {int(sab_delta.get('segments_found', 0)):,}")
        print(f"Segments read this run     : {int(sab_delta.get('segments_read', 0)):,}")
        print(f"Lifecycle events imported  : {int(sab_delta.get('events', 0)):,}")
        print(f"Attempts reconstructed     : {int(diagnostics_rebuilt):,}")
        print(f"Cancellation candidates    : {int(cancel_backfill.get('remove_candidates', 0)):,} remove; {int(cancel_backfill.get('purge_true_candidates', 0)):,} purge(true); {int(cancel_backfill.get('accepted', 0)):,} accepted")
        print(f"Cancellation reconciliation: {int(cancel_backfill.get('inserted', 0)):,} inserted; {int(cancel_backfill.get('reclassified', 0)):,} reclassified; {int(cancel_backfill.get('already_recorded', 0)):,} already recorded")
        cause_rows = conn.execute(
            """SELECT COALESCE(failure_stage,'Unknown') stage,COUNT(*) n
               FROM sab_attempt_diagnostics WHERE final_status='failed' GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        if cause_rows:
            print("Failure-stage totals       : " + ", ".join(f"{r['stage']}={r['n']}" for r in cause_rows))
        print_sab_summary(conn)
        print_prowlarr_indexers(indexers, stat_rows)
        print_history_import(imported_history, hist_types)
        print_prowlarr_log_delta(prow_log_delta)
        print_arr_log_delta(sonarr_delta)
        print_arr_log_delta(radarr_delta)
        print_correlation(conn)
        print_attributed_outcomes(conn, max(1, args.match_window_hours))
        print_prowlarr_diagnostics(conn)

        if args.debug_prowlarr:
            print("\nPROWLARR API SHAPE DIAGNOSTIC")
            print("-" * 78)
            print("Indexer count (Usenet only):", len(indexers))
            print("IndexerStats top-level type:", type(prow_stats_raw).__name__)
            if isinstance(prow_stats_raw, dict):
                print("IndexerStats keys:", sorted(prow_stats_raw.keys()))
            stats_rows = extract_stats_rows(prow_stats_raw)
            if stats_rows:
                print("First stats row keys:", sorted(stats_rows[0].keys()))
            print("History rows fetched:", len(prow_history_raw))
            if prow_history_raw:
                first = prow_history_raw[0]
                print("First history row keys:", sorted(first.keys()))
                print("First history eventType:", repr(first.get("eventType")))
                data = first.get("data")
                if isinstance(data, dict):
                    print("First history data keys:", sorted(data.keys()))

                grabbed = next(
                    (
                        r for r in prow_history_raw
                        if str(r.get("eventType", "")).lower() == "releasegrabbed"
                    ),
                    None,
                )
                if grabbed:
                    print("releaseGrabbed row keys:", sorted(grabbed.keys()))
                    gdata = grabbed.get("data")
                    if isinstance(gdata, dict):
                        print("releaseGrabbed data keys:", sorted(gdata.keys()))
                    extracted = history_title(grabbed)
                    print("releaseGrabbed extracted title present:", bool(extracted))
                    if extracted:
                        print("releaseGrabbed extracted title sample:", repr(extracted[:160]))

                grabbed_total = sum(
                    1 for r in prow_history_raw
                    if str(r.get("eventType", "")).lower() == "releasegrabbed"
                )
                grabbed_with_title = sum(
                    1 for r in prow_history_raw
                    if str(r.get("eventType", "")).lower() == "releasegrabbed"
                    and bool(history_title(r))
                )
                print("releaseGrabbed total:", grabbed_total)
                print("releaseGrabbed with extracted title:", grabbed_with_title)

        print("\nDATABASE")
        print("-" * 58)
        for label, table in (
            ("SAB provider snapshots", "provider_snapshots"),
            ("SAB attempt outcomes", "jobs"),
            ("SAB history diagnostics", "sab_history"),
            ("Prowlarr stat snapshots", "prowlarr_stats_snapshots"),
            ("Prowlarr history rows", "prowlarr_history"),
            ("Prowlarr log searches", "prowlarr_log_searches"),
            ("Prowlarr diagnostics", "prowlarr_log_errors"),
            ("Prowlarr log segments", "prowlarr_log_segments"),
            ("Arr orchestration events", "arr_events"),
            ("Search quality candidates", "arr_search_candidates"),
            ("Arr log segments", "arr_log_segments"),
        ):
            n = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
            print(f"{label:<29}: {int(n):,}")

        conn.close()

    except (sqlite3.Error, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 78)
    if source_errors:
        print("Collection finished with unavailable API sources; retained logs were still imported.", file=sys.stderr)
        return 3
    return 0


def main() -> int:
    args = parse_args()
    try:
        # Every entry point (web, CLI, backfill and rebuild) uses the same lock.
        with collection_lock(Path(args.db).expanduser().resolve()):
            return _main(args)
    except (RuntimeError, OSError, ValueError) as exc:
        secrets = [args.api_key, args.prowlarr_api_key, args.sonarr_api_key, args.radarr_api_key]
        print("ERROR: " + redact(str(exc), secrets), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
