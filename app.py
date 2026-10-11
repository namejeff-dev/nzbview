from __future__ import annotations

import math
import sqlite3
import time
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any
import threading
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import MonitorDB, normalize_identity
from collector_runner import CollectorRunner
from config import config_path, database_path, read_config
from runtime import local_now
from security import access_token, install_security


BASE = Path(__file__).parent
COLLECTOR_CONFIG = config_path()
DB_PATH = database_path(COLLECTOR_CONFIG)
try:
    _settings = read_config(COLLECTOR_CONFIG) if COLLECTOR_CONFIG.is_file() else {}
except (ValueError, OSError):
    _settings = {}
try:
    _attribution_hours = int(_settings.get("match_window_hours", 168))
except (TypeError, ValueError):
    _attribution_hours = 168
if _settings.get("log_timezone"):
    os.environ["NZBVIEW_LOG_TIMEZONE"] = str(_settings["log_timezone"])
db = MonitorDB(DB_PATH, _attribution_hours)
collector_runner = CollectorRunner(COLLECTOR_CONFIG, DB_PATH)

app = FastAPI(title="SABnzbd + Prowlarr + Arr Monitor", version="2.15.2")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
install_security(app)


def setup_response(request: Request, message: str = ""):
    return templates.TemplateResponse(request=request, name="setup.html", context={
        "request": request, "message": message,
        "db_path": str(DB_PATH), "config_path": str(COLLECTOR_CONFIG),
        "collector_configured": collector_runner.configured(),
        "collector_config_message": collector_runner.config_message(),
    })


@app.middleware("http")
async def missing_database(request: Request, call_next):
    try:
        return await call_next(request)
    except FileNotFoundError:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "Database not initialized; complete setup."}, status_code=503)
        return setup_response(request, "The database has not been created yet.")
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc) and "no such column" not in str(exc):
            raise
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "Database schema needs initialization or upgrade."}, status_code=503)
        return setup_response(request, "Run the collector once to initialize or upgrade the database schema.")


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    return setup_response(request)


NAV_GROUPS = [
    ("Monitor", [
        ("dashboard", "Dashboard", "📊", "/"),
        ("activity", "Activity", "🧭", "/activity"),
    ]),
    ("Analytics", [
        ("indexers", "Indexers", "🔎", "/indexers"),
        ("providers", "Providers", "🌐", "/providers"),
        ("downloads", "Downloads", "📥", "/downloads"),
        ("releases", "Releases", "📦", "/releases"),
        ("recovered", "Recovered", "♻️", "/recovered"),
        ("failures", "Failures", "❌", "/failures"),
    ]),
    ("System", [
        ("diagnostics", "Diagnostics", "🩺", "/diagnostics"),
        ("help", "Help", "💡", "/help"),
    ]),
]


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def age_text(value: str | None) -> tuple[str, str]:
    if not value:
        return "unknown", "warn"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        sec = max(0, int((now - dt).total_seconds()))
    except Exception:
        return "unknown", "warn"

    if sec < 60:
        text = f"{sec}s ago"
    elif sec < 3600:
        text = f"{sec // 60}m ago"
    elif sec < 86400:
        text = f"{sec // 3600}h ago"
    else:
        text = f"{sec // 86400}d ago"

    state = "good" if sec <= 15 * 60 else "warn" if sec <= 60 * 60 else "bad"
    return text, state


def collector_status(snap: dict[str, Any]) -> dict[str, Any]:
    age, state = age_text(snap["last_collection"].get("collected_at"))
    return {
        "age": age,
        "state": state,
        "time": snap["last_collection"].get("collected_at"),
        "completed": snap["last_collection"].get("sab_live_completed", snap["last_collection"].get("sab_completed", 0)),
        "failed": snap["last_collection"].get("sab_live_failed", snap["last_collection"].get("sab_failed", 0)),
    }


def render(request: Request, template: str, context: dict[str, Any], page_key: str, title: str):
    snap = context["snap"]
    base_context = {
        **context,
        "request": request,
        "nav_groups": NAV_GROUPS,
        "active": page_key,
        "page_title": title,
        "db_path": str(DB_PATH),
        "is_htmx": is_htmx(request),
        "collector": collector_status(snap),
        "collector_configured": collector_runner.configured(),
        "collector_config_message": collector_runner.config_message(),
        "sidebar_badges": {
            "downloads": snap["sab"]["attempts"],
            "failures": snap["sab"]["failed_only"],
        },
    }
    if is_htmx(request):
        return templates.TemplateResponse(request=request, name=template, context=base_context)
    base_context["content_template"] = template
    return templates.TemplateResponse(request=request, name="base.html", context=base_context)


_CACHE_LOCK = threading.Lock()
# Reuse analytical results for at most 60 seconds, or until SQLite changes.
# Time-window populations must expire even when the collector is idle.
_SNAP_CACHE: dict[tuple[int, int, int], dict[str, Any]] = {}
_ACTIVITY_CACHE: dict[tuple[int, int, int], dict[str, Any]] = {}
_SAMPLE_CACHE: dict[tuple[str, int, int, int], dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 60

def _db_generation() -> int:
    stamps = []
    for path in (DB_PATH, Path(str(DB_PATH) + "-wal")):
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            pass
    return max(stamps, default=0)

def _cached(cache: dict, hours: int, loader):
    generation = _db_generation()
    key = (int(hours), generation, int(time.monotonic() // _CACHE_TTL_SECONDS))
    with _CACHE_LOCK:
        hit = cache.get(key)
        if hit is not None:
            return hit

    value = loader()

    with _CACHE_LOCK:
        # Keep different activity-window results for the current DB generation so
        # switching 7d -> 14d -> 7d does not immediately force another rebuild.
        # Old generations are discarded lazily as soon as fresh work completes.
        stale = [k for k in cache if k[1:] != key[1:]]
        for stale_key in stale:
            cache.pop(stale_key, None)
        cache[key] = value
    return value

def get_snap(hours: int):
    return _cached(_SNAP_CACHE, hours, lambda: db.snapshot(hours))

def get_activity(hours: int):
    return _cached(_ACTIVITY_CACHE, hours, lambda: db.activity(hours))

def get_sample(population: str, sample_n: int):
    population = str(population or "downloads").lower()
    sample_n = int(sample_n or 100)
    generation = _db_generation()
    key = (population, sample_n, generation, int(time.monotonic() // _CACHE_TTL_SECONDS))
    with _CACHE_LOCK:
        hit = _SAMPLE_CACHE.get(key)
        if hit is not None:
            return hit
    value = db.sample_snapshot(population, sample_n)
    with _CACHE_LOCK:
        stale = [k for k in _SAMPLE_CACHE if k[2:] != key[2:]]
        for stale_key in stale:
            _SAMPLE_CACHE.pop(stale_key, None)
        _SAMPLE_CACHE[key] = value
    return value

def make_sample_scope(scope: str, sample_type: str, sample_n: int, allowed: list[str]) -> dict[str, Any]:
    mode = "sample" if str(scope).lower() == "sample" else "time"
    allowed = [x for x in allowed if x in {"searches", "grabs", "downloads"}]
    if not allowed:
        allowed = ["downloads"]
    sample_type = str(sample_type or allowed[0]).lower()
    if sample_type not in allowed:
        sample_type = allowed[0]
    sample_n = int(sample_n or 100)
    if sample_n not in {50, 100, 250, 500, 1000}:
        sample_n = 100
    return {
        "enabled": True, "mode": mode, "type": sample_type,
        "n": sample_n, "sizes": [50, 100, 250, 500, 1000],
        "types": allowed,
    }


def contains(text: str, q: str) -> bool:
    hay = normalize_identity(normalize_search_text(text))
    needles = normalize_identity(normalize_search_text(q)).split()
    return all(token in hay for token in needles)


def normalize_search_text(value: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]+", " ", str(value))


def page_rows(rows: list[dict[str, Any]], page: int, per_page: int = 50):
    total = len(rows)
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return rows[start:start + per_page], {
        "page": page,
        "pages": pages,
        "total": total,
        "per_page": per_page,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def sort_rows(
    rows: list[dict[str, Any]],
    sort: str,
    direction: str,
    mapping: dict[str, Any],
    default_sort: str,
):
    sort = sort if sort in mapping else default_sort
    direction = "asc" if str(direction).lower() == "asc" else "desc"
    keyfn = mapping[sort]

    def safe_key(item):
        value = keyfn(item)
        if value is None:
            return (1, "")
        if isinstance(value, str):
            return (0, value.lower())
        return (0, value)

    return sorted(rows, key=safe_key, reverse=(direction == "desc")), sort, direction


def qs(**values):
    return urlencode({k: v for k, v in values.items() if v is not None and v != ""})


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, hours: int = Query(168, ge=1, le=1440)):
    snap = get_snap(hours)
    return render(request, "dashboard.html", {"snap": snap}, "dashboard", "Dashboard")


@app.get("/activity", response_class=HTMLResponse)
def activity(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    tab: str = "overview",
    q: str = "",
    origin: list[str] = Query(default=[]),
    app_name: list[str] = Query(default=[], alias="app"),
    indexer: list[str] = Query(default=[]),
    outcome: list[str] = Query(default=[]),
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)
    data = get_activity(hours)
    allowed = {"overview", "searches", "handoffs", "downloads", "imports", "pre_sab", "infrastructure", "history", "unresolved", "cancelled"}
    tab = tab if tab in allowed else "overview"
    rows = list(data.get(tab, data.get("overview", [])))

    if q:
        rows = [x for x in rows if contains(" ".join(str(x.get(k) or "") for k in ("media","release","indexer","detail","stage","outcome")), q)]
    origins = {v.lower() for v in origin if v and v.lower() != "all"}
    apps = {v.lower() for v in app_name if v and v.lower() != "all"}
    indexers = {v.lower() for v in indexer if v and v.lower() != "all"}
    outcomes = {v.lower() for v in outcome if v and v.lower() != "all"}
    if origins:
        rows = [x for x in rows if str(x.get("origin") or "").lower() in origins]
    if apps:
        rows = [x for x in rows if str(x.get("app") or "").lower() in apps]
    if indexers:
        rows = [x for x in rows if indexers.intersection({v.strip().lower() for v in str(x.get("indexer") or "").split(",")})]
    if outcomes:
        rows = [x for x in rows if str(x.get("outcome") or "").lower() in outcomes]

    rows, pager = page_rows(rows, page, per_page=75)
    query_pairs = [("hours", str(hours)), ("tab", tab)]
    if q:
        query_pairs.append(("q", q))
    query_pairs += [("origin", v) for v in sorted(origins)]
    query_pairs += [("app", v) for v in sorted(apps)]
    query_pairs += [("indexer", v) for v in sorted(indexers)]
    query_pairs += [("outcome", v) for v in sorted(outcomes)]
    activity_base_query = urlencode(query_pairs)
    return render(
        request, "activity.html",
        {
            "snap": snap, "activity": data, "rows": rows, "pager": pager,
            "filters": {"tab": tab, "q": q, "origin": sorted(origins), "app": sorted(apps), "indexer": sorted(indexers), "outcome": sorted(outcomes), "base_query": activity_base_query},
        },
        "activity", "Activity",
    )


@app.get("/indexers", response_class=HTMLResponse)
def indexers(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    scope: str = "time",
    sample_type: str = "grabs",
    sample_n: int = 100,
):
    snap = get_snap(hours)
    # Indexer Performance has one fixed-count meaning: evaluate each indexer on
    # its own newest N grabs. Searches live in Raw Search Results and terminal
    # SAB attempts live in Downloads, avoiding three overlapping interpretations.
    sample_scope = make_sample_scope(scope, "grabs", sample_n, ["grabs"])
    sample = get_sample("indexer_grabs", sample_scope["n"]) if sample_scope["mode"] == "sample" else None
    return render(request, "indexers.html", {"snap": snap, "sample_scope": sample_scope, "sample": sample}, "indexers", "Indexers")


@app.get("/raw-search-results", response_class=HTMLResponse)
@app.get("/search-coverage", response_class=HTMLResponse, include_in_schema=False)
def raw_search_results(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    scope: str = "time",
    sample_type: str = "searches",
    sample_n: int = 100,
    q: str = "",
    gap: str = "all",
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)
    sample_scope = make_sample_scope(scope, "searches", sample_n, ["searches"])
    sample = get_sample("searches", sample_scope["n"]) if sample_scope["mode"] == "sample" else None
    coverage = sample.get("search_coverage", {}) if sample is not None else snap.get("search_coverage", {})
    rows = list(coverage.get("sessions", []))
    if q:
        rows = [
            x for x in rows
            if contains(
                " ".join([
                    str(x.get("media") or ""),
                    str(x.get("app") or ""),
                    str(x.get("target_label") or ""),
                    " ".join(i.get("indexer", "") for i in x.get("indexers", [])),
                ]),
                q,
            )
        ]
    if gap == "target_miss":
        rows = [x for x in rows if x.get("classification") == "global_target_miss"]
    elif gap == "partial":
        rows = [x for x in rows if x.get("classification") == "partial_target_coverage"]
    elif gap == "target_hit":
        rows = [x for x in rows if x.get("classification") in {"target_covered", "partial_target_coverage"}]
    elif gap == "unresolved":
        rows = [x for x in rows if not x.get("target_known")]
    elif gap == "2160":
        rows = [x for x in rows if "2160p" in (x.get("target_tiers") or [])]
    elif gap == "1080":
        rows = [x for x in rows if "1080p" in (x.get("target_tiers") or [])]
    rows, pager = page_rows(rows, page, per_page=50)
    return render(
        request, "search_coverage.html",
        {
            "snap": snap, "coverage": coverage, "rows": rows, "pager": pager,
            "sample_scope": sample_scope, "sample": sample,
            "filters": {"q": q, "gap": gap},
            "base_query": qs(hours=hours, scope=sample_scope["mode"] if sample_scope["mode"] == "sample" else None, sample_type="searches" if sample_scope["mode"] == "sample" else None, sample_n=sample_scope["n"] if sample_scope["mode"] == "sample" else None, q=q, gap=gap),
        },
        "indexers", "Indexers",
    )


@app.get("/providers", response_class=HTMLResponse)
def providers(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    scope: str = "time",
    sample_type: str = "downloads",
    sample_n: int = 100,
):
    snap = get_snap(hours)
    sample_scope = make_sample_scope(scope, "downloads", sample_n, ["downloads"])
    # Providers samples are selected directly from terminal SAB History rows that
    # retained a Servers contribution split. Do not choose N generic downloads
    # first and discard rows without provider evidence afterwards.
    sample = get_sample("provider_downloads", sample_scope["n"]) if sample_scope["mode"] == "sample" else None
    return render(request, "providers.html", {"snap": snap, "sample_scope": sample_scope, "sample": sample}, "providers", "Providers")


def group_download_attempts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the Downloads presentation hierarchy.

    A retained Sonarr whole-season search owns the automatic continuation it
    created.  Direct hand-offs establish the parent; later automatic/automatic-
    retry episode searches for the same series+season are folded back into that
    open acquisition for up to 24 hours.  Interactive/manual episode searches
    remain independent.  This keeps Sonarr redownload/recovery rounds inside the
    season that caused them instead of producing duplicate top-level episodes.
    """
    work = [dict(x) for x in rows]

    def parse_dt(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def sonarr_episode_slot(row: dict[str, Any]) -> tuple[str, int, int] | None:
        if str(row.get("media_app") or "").lower() != "sonarr":
            return None
        title = normalize_identity(str(row.get("media_title") or ""))
        season = row.get("media_season")
        episode = row.get("media_episode")
        if not title or season is None or episode is None:
            return None
        try:
            return title, int(season), int(episode)
        except (TypeError, ValueError):
            return None

    def season_slot(row: dict[str, Any], *, parent: bool = False) -> tuple[str, int] | None:
        title = row.get("season_search_title") if parent else row.get("media_title")
        season = row.get("season_search_season") if parent else row.get("media_season")
        title = normalize_identity(str(title or row.get("media_title") or ""))
        if not title or season is None:
            return None
        try:
            return title, int(season)
        except (TypeError, ValueError):
            return None

    season_fields = (
        "season_search_id", "season_search_title", "season_search_season",
        "season_search_started", "season_search_completed", "season_search_reports_downloaded",
        "season_search_mode", "season_search_confidence",
    )

    # Build authoritative season-acquisition anchors from attempts that were directly
    # handed off by an observed Sonarr season search.  The exact media_key is not used
    # here: fallback/correlation differences can change that key across retry rounds
    # even though Sonarr is clearly retrying the same canonical episode.
    batches: dict[str, dict[str, Any]] = {}
    batches_by_season: dict[tuple[str, int], list[dict[str, Any]]] = {}
    failed_slots: dict[tuple[str, int, int], list[datetime]] = {}
    for row in work:
        slot = sonarr_episode_slot(row)
        rdt = parse_dt(row.get("time"))
        if slot and rdt and str(row.get("status") or "").lower() == "failed":
            failed_slots.setdefault(slot, []).append(rdt)

        batch_id = str(row.get("season_search_id") or "")
        if not batch_id:
            continue
        anchor = batches.get(batch_id)
        if anchor is None:
            sslot = season_slot(row, parent=True)
            started = parse_dt(row.get("season_search_started")) or rdt
            anchor = {
                "id": batch_id,
                "season_slot": sslot,
                "started": started,
                "row": row,
                "episode_slots": set(),
                "completed_slots": set(),
                "has_season_pack": False,
                "season_pack_completed": False,
            }
            batches[batch_id] = anchor
            if sslot:
                batches_by_season.setdefault(sslot, []).append(anchor)
        if slot:
            anchor["episode_slots"].add(slot)
            if str(row.get("status") or "").lower() == "completed":
                anchor["completed_slots"].add(slot)
        elif row.get("media_season") is not None and row.get("media_episode") is None:
            anchor["has_season_pack"] = True
            if str(row.get("status") or "").lower() == "completed":
                anchor["season_pack_completed"] = True

    for anchors in batches_by_season.values():
        anchors.sort(key=lambda a: a["started"].timestamp() if a.get("started") else 0)

    # Keep an observed season acquisition open while Sonarr automatically works
    # through it.  Explicit automatic/automatic-retry episode searches for the same
    # series+season belong to the latest open season batch.  Rows without retained
    # search metadata are inherited only when the episode itself was already part of
    # that batch (or has an earlier failed attempt), which keeps weak evidence from
    # swallowing unrelated activity.
    for row in sorted(work, key=lambda x: str(x.get("time") or "")):
        if row.get("season_search_id"):
            continue
        slot = sonarr_episode_slot(row)
        if not slot:
            continue
        sslot = (slot[0], slot[1])
        anchors = batches_by_season.get(sslot, [])
        if not anchors:
            continue
        rdt = parse_dt(row.get("time"))
        if rdt is None:
            continue

        mode = str(row.get("arr_search_mode") or "").lower()
        has_search = bool(row.get("arr_search_id"))
        is_episode_search = row.get("arr_search_episode") is not None

        # Interactive/manual/browse searches must remain their own acquisition.
        if has_search and mode not in {"automatic", "automatic_retry"}:
            continue
        if has_search and not is_episode_search:
            continue

        candidates: list[tuple[float, dict[str, Any]]] = []
        for anchor in anchors:
            started = anchor.get("started")
            if started is None:
                continue
            delta = (rdt - started).total_seconds()
            if not (-120 <= delta <= 24 * 3600):
                continue

            # The original directly-observed episode set is authoritative. Later
            # automatic searches may continue only episodes that belonged to that
            # season acquisition. A failed season-pack is the exception: Sonarr may
            # legitimately fall back to individual episode searches after the pack.
            target_episode = slot in anchor["episode_slots"]
            pack_fallback = bool(anchor["has_season_pack"] and not anchor["season_pack_completed"])
            if not (target_episode or pack_fallback):
                continue

            # Once every original episode has eventually completed (or the season
            # pack completed), the acquisition is terminal and a later automatic
            # search should start a new top-level action rather than being swallowed.
            if anchor["season_pack_completed"]:
                continue
            if anchor["episode_slots"] and anchor["completed_slots"].issuperset(anchor["episode_slots"]):
                continue

            if has_search:
                # A retained automatic episode search is the strongest continuation
                # signal available after the original season search closes. This is
                # how Sonarr's RedownloadFailedDownloadService flow appears even when
                # the retained mode is plain ``automatic`` rather than retry.
                eligible = mode in {"automatic", "automatic_retry"}
            else:
                prior_failure = any(fdt <= rdt for fdt in failed_slots.get(slot, []))
                eligible = target_episode and prior_failure
            if eligible:
                # Prefer the most recent season acquisition when two searches of the
                # same season occur inside the 24-hour safety window.
                candidates.append((delta, anchor))

        if not candidates:
            continue
        parent = max(candidates, key=lambda x: x[1]["started"].timestamp() if x[1].get("started") else 0)[1]
        parent_row = parent["row"]
        for field in season_fields:
            row[field] = parent_row.get(field)
        row["season_search_inherited"] = True
        row["season_search_inherited_reason"] = (
            "automatic retry" if mode == "automatic_retry" else
            "automatic season continuation" if has_search else
            "same season acquisition"
        )
        if str(row.get("status") or "").lower() == "completed":
            parent["completed_slots"].add(slot)

    top: dict[str, dict[str, Any]] = {}

    def status_bucket(group: dict[str, Any], row: dict[str, Any]) -> None:
        status = str(row.get("status") or "unresolved").lower()
        if status == "completed":
            group["completed"] += 1
        elif status == "failed":
            group["failed"] += 1
        elif status == "cancelled":
            group["cancelled"] += 1
        else:
            group["unresolved"] += 1
        group["repair_max"] = max(int(group.get("repair_max") or 0), int(row.get("repair_blocks") or 0))
        if str(row.get("time") or "") > str(group.get("latest_time") or ""):
            group["latest_time"] = row.get("time") or ""
        idx = str(row.get("indexer") or "(unattributed)")
        if idx not in group["indexers"]:
            group["indexers"].append(idx)

    def new_media_group(key: str, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "media", "key": key,
            "media": row.get("media_label") or row.get("title") or "Unknown media",
            "media_title": row.get("media_title") or row.get("media_label") or row.get("title") or "Unknown media",
            "media_source": row.get("media_source") or "release fallback",
            "media_app": row.get("media_app") or "unknown",
            "media_season": row.get("media_season"), "media_episode": row.get("media_episode"),
            "is_season_pack": bool(row.get("media_is_season_pack")),
            "attempts": [], "completed": 0, "failed": 0, "cancelled": 0, "unresolved": 0,
            "repair_max": 0, "latest_time": row.get("time") or "", "indexers": [],
        }

    for row in work:
        batch_id = str(row.get("season_search_id") or "")
        if batch_id:
            tkey = f"season-search:{batch_id}"
            group = top.get(tkey)
            if group is None:
                season = int(row.get("season_search_season") or row.get("media_season") or 0)
                title = str(row.get("season_search_title") or row.get("media_title") or "TV series")
                group = {
                    "kind": "season", "key": tkey, "media": f"{title} · Season {season}",
                    "media_title": title, "media_app": "sonarr", "season": season,
                    "season_search_started": row.get("season_search_started"),
                    "season_search_completed": row.get("season_search_completed"),
                    "season_search_reports_downloaded": row.get("season_search_reports_downloaded"),
                    "season_search_mode": row.get("season_search_mode") or "unknown",
                    "season_search_confidence": row.get("season_search_confidence") or "none",
                    "children_map": {}, "attempts": [], "completed": 0, "failed": 0,
                    "cancelled": 0, "unresolved": 0, "repair_max": 0,
                    "latest_time": row.get("time") or "", "indexers": [],
                }
                top[tkey] = group
            group["attempts"].append(row)
            status_bucket(group, row)
            # Inside an observed season acquisition the episode identity is the
            # stable grouping key. Correlation/fallback differences can give retry
            # attempts different media_key values even though they are the same
            # SxxExx, which previously produced duplicate nested/top-level rows.
            if row.get("media_episode") is not None:
                ep = int(row.get("media_episode") or 0)
                ckey = f"episode:{int(group['season']):02d}:{ep:03d}"
            else:
                ckey = "season-pack"
            child = group["children_map"].get(ckey)
            if child is None:
                child = new_media_group(ckey, row)
                child["kind"] = "season_pack" if row.get("media_episode") is None else "episode"
                if child["kind"] == "episode":
                    ep = int(row.get("media_episode") or 0)
                    child["media"] = f"S{int(group['season']):02d}E{ep:02d}"
                else:
                    child["media"] = "Season pack"
                group["children_map"][ckey] = child
            child["attempts"].append(row)
            status_bucket(child, row)
        else:
            key = str(row.get("media_key") or normalize_identity(row.get("media_label") or row.get("title") or "unknown"))
            group = top.get(key)
            if group is None:
                group = new_media_group(key, row)
                top[key] = group
            group["attempts"].append(row)
            status_bucket(group, row)

    def finish(group: dict[str, Any]) -> None:
        group["attempts"].sort(key=lambda x: str(x.get("time") or ""))
        group["attempt_count"] = len(group["attempts"])
        group["known_count"] = int(group["completed"]) + int(group["failed"])
        group["eventually_completed"] = bool(group["completed"])
        completed_times = [
            str(x.get("time") or "") for x in group["attempts"]
            if str(x.get("status") or "").lower() == "completed" and x.get("time")
        ]
        group["completed_time"] = max(completed_times) if completed_times else None
        group["final_time"] = group["completed_time"] if group["eventually_completed"] else group.get("latest_time")
        group["time_kind"] = "completed" if group["eventually_completed"] else "last activity"
        if group["completed"] and group["failed"]:
            group["summary_status"] = "mixed"
        elif group["failed"] and not group["completed"]:
            group["summary_status"] = "failed"
        elif group["completed"] and not group["failed"]:
            group["summary_status"] = "completed"
        elif group["cancelled"]:
            group["summary_status"] = "cancelled"
        else:
            group["summary_status"] = "unresolved"
        group["needs_details"] = group["attempt_count"] > 1 or bool(group["failed"] or group["cancelled"] or group["unresolved"])

    out = []
    for group in top.values():
        finish(group)
        if group["kind"] == "season":
            children = list(group.pop("children_map").values())
            for child in children:
                finish(child)
            children.sort(key=lambda x: (
                0 if x["kind"] == "episode" else 1,
                int(x.get("media_episode") or 9999),
                str(x.get("latest_time") or ""),
            ))
            group["children"] = children
            episodes = [x for x in children if x["kind"] == "episode"]
            packs = [x for x in children if x["kind"] == "season_pack"]
            group["episode_count"] = len(episodes)
            group["episode_completed"] = sum(1 for x in episodes if x["eventually_completed"])
            group["episode_failed_final"] = sum(1 for x in episodes if not x["eventually_completed"] and x["failed"] > 0)
            group["season_pack_count"] = len(packs)
            group["season_pack_completed"] = sum(1 for x in packs if x["eventually_completed"])
            group["failed_attempts"] = sum(int(x["failed"]) for x in children)

            # Season-level completion is an acquisition outcome, not merely "at least
            # one child completed". A completed season pack is sufficient; otherwise
            # every observed episode child must eventually complete. The timestamp
            # shown in the collapsed parent is when that final successful child
            # completed. If the season is still incomplete, show the latest activity
            # time instead and label it accordingly.
            episode_complete = bool(episodes) and group["episode_completed"] == group["episode_count"]
            pack_complete = bool(packs) and group["season_pack_completed"] > 0
            group["eventually_completed"] = bool(episode_complete or pack_complete)
            child_completed_times = [str(x.get("completed_time") or "") for x in children if x.get("completed_time")]
            group["completed_time"] = max(child_completed_times) if group["eventually_completed"] and child_completed_times else None
            group["final_time"] = group["completed_time"] if group["eventually_completed"] else group.get("latest_time")
            group["time_kind"] = "completed" if group["eventually_completed"] else "last activity"
        out.append(group)

    out.sort(key=lambda x: str(x.get("latest_time") or ""), reverse=True)
    return out


@app.get("/downloads", response_class=HTMLResponse)
def downloads(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    scope: str = "time",
    sample_type: str = "downloads",
    sample_n: int = 100,
    q: str = "",
    status: str = "all",
    outcome: str = "all",
    indexer: list[str] = Query(default=[]),
    match: str = "all",
    repair: str = "all",
    sort: str = "time",
    dir: str = "desc",
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)
    sample_scope = make_sample_scope(scope, "downloads", sample_n, ["downloads"])
    sample = get_sample("downloads", sample_scope["n"]) if sample_scope["mode"] == "sample" else None
    rows = list(sample.get("attempts", [])) if sample is not None else list(snap["attempts"])

    if q:
        rows = [x for x in rows if contains(x.get("title", ""), q) or contains(x.get("media_label", ""), q)]
    if status != "all":
        rows = [x for x in rows if x["status"] == status]
    # Downloads is attempt-centric.  A later release/acquisition outcome is
    # shown only as secondary context and is not a primary filter here.
    outcome = "all"
    selected_indexers = {v.lower() for v in indexer if v and v.lower() != "all"}
    if selected_indexers:
        rows = [x for x in rows if x["indexer"].lower() in selected_indexers]
    if match != "all":
        rows = [x for x in rows if x["match_method"] == match]
    if repair == "repaired":
        rows = [x for x in rows if x["repair_blocks"] > 0]
    elif repair == "heavy":
        rows = [x for x in rows if x["repair_blocks"] >= 10]

    rows, sort, dir = sort_rows(rows, sort, dir, {
        "time": lambda x: x["time"],
        "attempt": lambda x: x["status"],
        "outcome": lambda x: x["release_outcome"],
        "indexer": lambda x: x["indexer"],
        "match": lambda x: x["match_method"],
        "release": lambda x: x["title"],
        "repair": lambda x: int(x["repair_blocks"] or 0),
    }, "time")
    filtered_attempt_total = len(rows)
    groups = group_download_attempts(rows)
    groups, pager = page_rows(groups, page, per_page=12)
    filters = {
        "q": q, "status": status, "outcome": outcome,
        "indexer": sorted(selected_indexers), "indexer_query": "".join(f"&indexer={quote(v)}" for v in sorted(selected_indexers)), "match": match, "repair": repair,
        "sort": sort, "dir": dir,
    }
    return render(
        request, "downloads.html",
        {"snap": snap, "rows": rows, "download_groups": groups, "filtered_attempt_total": filtered_attempt_total,
         "filters": filters, "pager": pager, "sample_scope": sample_scope, "sample": sample,
         "filter_indexers": sample.get("indexers", []) if sample is not None else snap.get("indexers", [])},
        "downloads", "Downloads",
    )


@app.get("/releases", response_class=HTMLResponse)
def releases(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    q: str = "",
    status: str = "all",
    indexer: list[str] = Query(default=[]),
    match: str = "all",
    sort: str = "time",
    dir: str = "desc",
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)
    rows = snap["releases"]

    if q:
        rows = [x for x in rows if contains(x["title"], q)]
    if status == "completed":
        rows = [x for x in rows if x["eventually_completed"]]
    elif status == "failed":
        rows = [x for x in rows if x.get("outcome") == "failed"]
    elif status == "cancelled":
        rows = [x for x in rows if x.get("outcome") == "cancelled"]
    elif status == "recovered":
        rows = [x for x in rows if x["recovered"]]
    selected_indexers = {v.lower() for v in indexer if v and v.lower() != "all"}
    if selected_indexers:
        rows = [x for x in rows if x["indexer"].lower() in selected_indexers]
    if match != "all":
        rows = [x for x in rows if x["match_method"] == match]

    rows, sort, dir = sort_rows(rows, sort, dir, {
        "time": lambda x: x["final_time"],
        "outcome": lambda x: x["outcome"],
        "indexer": lambda x: x["indexer"],
        "match": lambda x: x["match_method"],
        "attempts": lambda x: int(x["attempts"] or 0),
        "release": lambda x: x["title"],
        "repair": lambda x: int(x["repair_blocks"] or 0),
    }, "time")
    rows, pager = page_rows(rows, page)
    filters = {
        "q": q, "status": status, "indexer": sorted(selected_indexers), "indexer_query": "".join(f"&indexer={quote(v)}" for v in sorted(selected_indexers)), "match": match,
        "sort": sort, "dir": dir,
    }
    return render(
        request, "releases.html",
        {"snap": snap, "rows": rows, "filters": filters, "pager": pager},
        "releases", "Releases",
    )


@app.get("/recovered", response_class=HTMLResponse)
def recovered(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    q: str = "",
    kind: str = "all",
    sort: str = "time",
    dir: str = "desc",
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)
    rows = snap["recoveries"]
    if q:
        rows = [x for x in rows if contains(x["title"], q)]
    if kind != "all":
        rows = [x for x in rows if x["kind"] == kind]

    rows, sort, dir = sort_rows(rows, sort, dir, {
        "time": lambda x: x.get("final_time") or "",
        "type": lambda x: x["kind"],
        "failed": lambda x: x["failed_via"],
        "recovered": lambda x: x["recovered_via"],
        "release": lambda x: x["title"],
        "repair": lambda x: int(x["repair_blocks"] or 0),
    }, "time")
    rows, pager = page_rows(rows, page)
    summary = {}
    for x in snap["recoveries"]:
        key = f"{x['failed_via']} → {x['recovered_via']}"
        summary[key] = summary.get(key, 0) + 1

    return render(
        request, "recovered.html",
        {
            "snap": snap, "rows": rows, "pager": pager,
            "filters": {"q": q, "kind": kind, "sort": sort, "dir": dir},
            "path_summary": sorted(summary.items(), key=lambda x: (-x[1], x[0])),
        },
        "recovered", "Recovered",
    )


@app.get("/failures", response_class=HTMLResponse)
def failures(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    q: str = "",
    indexer: list[str] = Query(default=[]),
    match: str = "all",
    repair: str = "all",
    cause: str = "all",
    sort: str = "time",
    dir: str = "desc",
    page: int = Query(1, ge=1),
):
    snap = get_snap(hours)

    # A Failures page should mean unresolved failures.  Failed attempts that
    # later recovered belong on Downloads/Recovered, not here.
    rows = [
        x for x in snap["attempts"]
        if x["status"] == "failed" and x["release_outcome"] == "failed"
    ]

    if q:
        rows = [x for x in rows if contains(x["title"], q)]
    selected_indexers = {v.lower() for v in indexer if v and v.lower() != "all"}
    if selected_indexers:
        rows = [x for x in rows if x["indexer"].lower() in selected_indexers]
    if match != "all":
        rows = [x for x in rows if x["match_method"] == match]
    if repair == "repaired":
        rows = [x for x in rows if x["repair_blocks"] > 0]
    elif repair == "heavy":
        rows = [x for x in rows if x["repair_blocks"] >= 10]
    if cause != "all":
        wanted = cause.lower()
        def cause_name(x):
            stage = str(x.get("failure_stage") or "Cause unavailable")
            if stage == "Unknown":
                stage = "Cause unavailable"
            return stage.lower()
        rows = [x for x in rows if cause_name(x) == wanted]

    rows, sort, dir = sort_rows(rows, sort, dir, {
        "time": lambda x: x["time"],
        "indexer": lambda x: x["indexer"],
        "match": lambda x: x["match_method"],
        "release": lambda x: x["title"],
        "repair": lambda x: int(x["repair_blocks"] or 0),
        "cause": lambda x: str(x.get("failure_stage") or "Unknown"),
    }, "time")
    rows, pager = page_rows(rows, page)
    filters = {
        "q": q, "indexer": sorted(selected_indexers), "indexer_query": "".join(f"&indexer={quote(v)}" for v in sorted(selected_indexers)), "match": match, "repair": repair, "cause": cause,
        "sort": sort, "dir": dir,
    }
    return render(
        request, "failures.html",
        {"snap": snap, "rows": rows, "filters": filters, "pager": pager},
        "failures", "Failures",
    )


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, hours: int = Query(168, ge=1, le=1440)):
    snap = get_snap(hours)
    return render(request, "diagnostics.html", {"snap": snap}, "diagnostics", "Diagnostics")


@app.get("/release-detail", response_class=HTMLResponse)
def release_detail(
    request: Request,
    title: str,
    hours: int = Query(168, ge=1, le=1440),
    anchor_time: str | None = None,
):
    snap = get_snap(max(hours, 1440))
    ident = normalize_identity(title)
    release = next((x for x in snap["releases"] if normalize_identity(x["title"]) == ident), None)
    recovery = next((x for x in snap["recoveries"] if normalize_identity(x["title"]) == ident), None)
    chain = db.release_chain(title, anchor_time=anchor_time)
    attempts = chain.get("attempts", [])

    # The drawer represents the contiguous acquisition journey. A retry may use a
    # different release title, so the release-level table row can legitimately have
    # fewer exact-title attempts than the reconstructed journey. Reflect the journey
    # in the drawer header without changing the table's release-level statistics.
    if release is not None and attempts:
        terminal = [a for a in attempts if str(a.get("status") or "").lower() in {"completed", "failed", "cancelled"}]
        if terminal:
            final_attempt = terminal[-1]
            statuses = [str(a.get("status") or "").lower() for a in terminal]
            release = dict(release)
            release["attempts"] = len(terminal)
            release["repair_blocks"] = max(int(a.get("repair_blocks") or 0) for a in terminal)
            release["final_status"] = str(final_attempt.get("status") or release.get("final_status") or "unresolved")
            release["final_time"] = final_attempt.get("time") or release.get("final_time")
            release["eventually_completed"] = "completed" in statuses
            release["recovered"] = "completed" in statuses and "failed" in statuses[:statuses.index("completed")]
            if release["recovered"]:
                release["outcome"] = "recovered"
            elif release["eventually_completed"]:
                release["outcome"] = "completed"

    # v2.9.1.1 can also open an Arr-only pre-SAB failure.  These releases never
    # became SAB attempts, so synthesize the small header model from the
    # orchestration evidence instead of returning an empty drawer.
    if release is None:
        arr_only = list(chain.get("arr_events", []))
        failed_arr = [x for x in arr_only if x.get("event_type") in {"grab_failed", "grab_error", "import_failed", "import_cancelled", "download_cancelled"}]
        sent_arr = [x for x in arr_only if x.get("event_type") == "sent_to_sab"]
        search_arr = [x for x in arr_only if x.get("event_type") == "search_started"]
        if failed_arr:
            final = max(failed_arr, key=lambda x: str(x.get("time") or ""))
            cancelled = final.get("event_type") in {"import_cancelled", "download_cancelled"}
            release = {
                "title": title, "recovered": False, "eventually_completed": False,
                "outcome": "cancelled" if cancelled else "failed",
                "indexer": final.get("indexer") or "(unattributed)",
                "match_method": "arr-" + str(final.get("match_method") or "media"),
                "failure_stage": "Cancelled" if cancelled else ("Pre-SAB" if final.get("event_type") in {"grab_failed", "grab_error"} else "Import"),
                "attempts": 0, "repair_blocks": 0,
                "final_status": "cancelled" if cancelled else ("grab failed" if final.get("event_type") in {"grab_failed", "grab_error"} else "import failed"),
                "final_time": final.get("time") or "",
            }
        elif sent_arr:
            final = max(sent_arr, key=lambda x: str(x.get("time") or ""))
            release = {
                "title": title, "recovered": False, "eventually_completed": False, "outcome": "unresolved",
                "indexer": final.get("indexer") or "(unattributed)", "match_method": "arr-exact",
                "failure_stage": None, "attempts": 0, "repair_blocks": 0,
                "final_status": "pending / unresolved", "final_time": final.get("time") or "",
            }
        elif search_arr:
            final = max(search_arr, key=lambda x: str(x.get("time") or ""))
            release = {
                "title": title, "recovered": False, "eventually_completed": False, "outcome": "search-only",
                "indexer": "search only", "match_method": "arr-media", "failure_stage": None,
                "attempts": 0, "repair_blocks": 0, "final_status": "no download selected",
                "final_time": final.get("time") or "",
            }

    return templates.TemplateResponse(
        request=request,
        name="release_detail.html",
        context={
            "request": request,
            "snap": snap,
            "release": release,
            "attempts": attempts,
            "recovery": recovery,
            "chain": chain,
            "title": title,
        },
    )




@app.get("/help", response_class=HTMLResponse)
def help_page(
    request: Request,
    hours: int = Query(168, ge=1, le=1440),
    topic: str = "overview",
):
    snap = get_snap(hours)
    allowed = {
        "overview", "dashboard", "activity", "indexers", "providers",
        "downloads", "releases", "recovered", "failures", "diagnostics",
    }
    topic = topic if topic in allowed else "overview"
    return render(request, "help.html", {"snap": snap, "help_topic": topic}, "help", "Help")

@app.get("/partials/status", response_class=HTMLResponse)
def partial_status(request: Request):
    snap = get_snap(168)
    return templates.TemplateResponse(
        request=request,
        name="_collector_status.html",
        context={"request": request, "collector": collector_status(snap)},
    )


@app.get("/api/collector/status")
def api_collector_status():
    return collector_runner.status()


@app.post("/api/collector/run")
def api_collector_run(request: Request):
    # Network-facing installations require the authenticated middleware above.
    host = request.client.host if request.client else ""
    if not access_token() and host not in {"127.0.0.1", "::1", "localhost"}:
        return JSONResponse(
            {"ok": False, "error": "Collector can only be started locally."},
            status_code=403,
        )
    try:
        state = collector_runner.start()
        return JSONResponse({"ok": True, **state}, status_code=202)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), **collector_runner.status()},
            status_code=400,
        )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(BASE / "static" / "nzbview_logo.svg", media_type="image/svg+xml")


@app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def chrome_probe():
    return Response(status_code=204)


@app.get("/api/health")
def api_health():
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        snap = get_snap(168)
        return {"ok": True, "version": "2.15.2", "collector": collector_status(snap)}
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "Database not initialized; complete setup."}, status_code=503)
    except Exception:
        return JSONResponse({"ok": False, "error": "Database check failed; check server diagnostics."}, status_code=500)
