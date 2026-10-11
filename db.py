from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from runtime import local_now, localize


EPISODE_RE = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,2})E(\d{1,3})(?!\d)")
SEASON_RE = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,2})(?!E\d{1,3})(?!\d)")


def pct(n: int | float, d: int | float, digits: int = 2) -> float | None:
    if not d:
        return None
    return round(float(n) / float(d) * 100.0, digits)


def human_bytes(value: int | float) -> str:
    value = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for i, unit in enumerate(units):
        if abs(value) < 1024.0 or i == len(units) - 1:
            return f"{int(value):,} B" if i == 0 else f"{value:.1f} {unit}"
        value /= 1024.0
    return "0 B"


def parse_human_bytes(value: str) -> int:
    """Parse SAB History's human-readable per-server byte totals.

    History stage_log currently reports values such as ``news.example.com=7.2 GB``.
    These are rounded display values, so contribution totals derived from them are
    intentionally presented as observed/approximate rather than byte-perfect API counters.
    """
    m = re.search(r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|TB|PB)\b", str(value or ""))
    if not m:
        return 0
    number = float(m.group(1))
    power = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4, "PB": 5}[m.group(2).upper()]
    return int(number * (1024 ** power))


def provider_label(host: str) -> str:
    """Derive a readable label without a fixed provider-name registry.

    SAB's server_stats key is commonly a hostname.  Prefer the first
    non-generic hostname token, while still allowing a configured/non-host
    server name to pass through.  This keeps new/replaced providers visible
    without code changes.
    """
    text = str(host or "").strip()
    if not text:
        return "Provider"
    low = text.lower().split(":", 1)[0]
    parts = [p for p in low.split(".") if p and p not in {"news", "usenet", "nntp", "server"}]
    if len(parts) >= 2:
        return parts[0].replace("-", " ").replace("_", " ").title()
    if "." not in text:
        return text
    return parts[0].replace("-", " ").replace("_", " ").title() if parts else text


def extract_server_contributions(stage_log: Any) -> dict[str, int]:
    """Extract approximate per-server bytes from SAB History ``Servers`` stages."""
    if isinstance(stage_log, str):
        try:
            stage_log = json.loads(stage_log or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(stage_log, list):
        return {}

    out: dict[str, int] = defaultdict(int)
    assignment_re = re.compile(
        r"(?i)(?:^|[,;·])\s*(?P<server>[^=,;·]+?)\s*=\s*(?P<size>[0-9]+(?:\.[0-9]+)?\s*(?:B|KB|MB|GB|TB|PB))\b"
    )
    for stage in stage_log:
        if not isinstance(stage, dict) or str(stage.get("name") or "").strip().lower() != "servers":
            continue
        actions = stage.get("actions") or []
        if not isinstance(actions, list):
            actions = [actions]
        for action in actions:
            text = re.sub(r"(?i)<br\s*/?>", ", ", str(action or ""))
            text = re.sub(r"<[^>]+>", "", text)
            for m in assignment_re.finditer(text):
                server = m.group("server").strip()
                size = parse_human_bytes(m.group("size"))
                if server and size > 0:
                    out[server] += size
    return dict(out)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_identity(name: str) -> str:
    return " ".join(str(name).strip().lower().split())


def normalize_match(name: str) -> str:
    name = str(name).strip().lower()
    name = re.sub(r"[\s._-]+", " ", name)
    name = re.sub(r"[^a-z0-9 ]+", "", name)
    return " ".join(name.split())


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


def season_identity(title: str) -> tuple[str, int] | None:
    """Return a conservative series/season identity for season-pack shaped releases."""
    text = str(title).strip()
    if EPISODE_RE.search(text):
        return None
    m = SEASON_RE.search(text)
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
    return series, int(m.group(1))


def arr_media_matches_release(
    app: str, media_title: str | None, season: int | None, episode: int | None, release_title: str
) -> bool:
    """Conservatively match an Arr media/search identity to a SAB release title."""
    if not media_title:
        return False
    app = str(app or "").lower()
    if app == "sonarr":
        rep = episode_identity(release_title)
        if rep:
            release_series, release_season, release_episode = rep
        else:
            pack = season_identity(release_title)
            if not pack:
                return False
            release_series, release_season = pack
            release_episode = None
        media_series = normalize_match(media_title)
        # Strip year noise in the same spirit as episode_identity().
        media_series = re.sub(r"\b(?:19|20)\d{2}\b", " ", media_series)
        media_series = " ".join(media_series.split())
        if media_series != release_series:
            return False
        if season is not None and int(season) != release_season:
            return False
        if episode is not None and release_episode is not None and int(episode) != release_episode:
            return False
        if episode is not None and release_episode is None:
            return False
        return True

    # Radarr's media title usually includes the movie title/year while the
    # release adds codec/source/group tokens.  Normalized substring matching
    # is deliberately one-way to avoid merging unrelated short titles.
    media = normalize_match(media_title)
    release = normalize_match(release_title)
    return bool(media and len(media) >= 4 and media in release)


def arr_release_matches(release_name: str | None, target_title: str) -> tuple[bool, str]:
    if not release_name:
        return False, "none"
    if normalize_match(release_name) == normalize_match(target_title):
        return True, "exact"
    a = episode_identity(release_name)
    b = episode_identity(target_title)
    if a is not None and b is not None and a == b:
        return True, "episode"
    return False, "none"


class MonitorDB:
    def __init__(self, path: Path, attribution_hours: int = 168):
        self.path = Path(path)
        self.attribution_hours = max(1, min(int(attribution_hours), 1440))

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(f"Database not found: {self.path}")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _latest_from_index(index, key, when, hours):
        best = None
        max_age = timedelta(hours=hours)
        for item in index.get(key, []):
            if item["dt"] > when:
                break
            age = when - item["dt"]
            if timedelta(0) <= age <= max_age:
                best = item
        return best

    def _grab_indexes(self, conn: sqlite3.Connection, since: datetime | None = None):
        """Build release->grab lookup indexes, optionally bounded by retained time.

        Attribution never needs every historical Prowlarr grab for a selected-window
        page.  Supplying ``since`` keeps the lookup proportional to the requested
        window while preserving the configured attribution look-back.
        """
        exact = defaultdict(list)
        episode = defaultdict(list)

        params: tuple[Any, ...] = ()
        since_sql = ""
        if since is not None:
            since_sql = " AND event_time >= ?"
            params = (since.isoformat(timespec="seconds"),)
        rows = conn.execute(
            f"""
            SELECT event_time,indexer_name,indexer_id,release_title
            FROM prowlarr_history
            WHERE lower(event_type) LIKE '%grab%'
              AND COALESCE(release_title,'') <> ''
              {since_sql}
            ORDER BY event_time
            """,
            params,
        ).fetchall()

        for r in rows:
            dt = parse_dt(r["event_time"])
            if dt is None:
                continue
            item = {
                "dt": dt,
                "time": r["event_time"],
                "indexer": r["indexer_name"] or str(r["indexer_id"]),
                "title": r["release_title"],
            }
            key = normalize_match(r["release_title"])
            if key:
                exact[key].append(item)
            ep = episode_identity(r["release_title"])
            if ep:
                episode[ep].append(item)

        return exact, episode

    def _attribute(self, exact, episode, title: str, event_time: str, hours: int):
        when = parse_dt(event_time)
        if when is None:
            return None, "unattributed"

        key = normalize_match(title)
        if key:
            hit = self._latest_from_index(exact, key, when, hours)
            if hit:
                return hit, "exact"

        ep = episode_identity(title)
        if ep:
            hit = self._latest_from_index(episode, ep, when, hours)
            if hit:
                return hit, "episode"

        return None, "unattributed"

    @staticmethod
    def _counter_delta(cur, prev, field):
        current = int(cur[field] or 0)
        if prev is None:
            return current
        previous = int(prev[field] or 0)
        return current - previous if current >= previous else current

    @staticmethod
    def _local_dt(value):
        dt = parse_dt(value)
        if dt is not None and dt.tzinfo is None:
            dt = localize(dt)
        return dt

    @classmethod
    def _window_baseline(cls, rows, cutoff):
        """Return (current, baseline, partial, current_is_in_window).

        Counters are cumulative.  We use the latest stored snapshot at or
        before the requested cutoff as the baseline.  If the database does
        not yet reach that far back, the earliest stored snapshot is used and
        the result is marked partial.
        """
        if not rows:
            return None, None, True, False

        current = rows[-1]
        current_dt = cls._local_dt(current["collected_at"])
        if current_dt is not None and current_dt < cutoff:
            return current, current, False, False

        baseline = None
        for row in rows:
            dt = cls._local_dt(row["collected_at"])
            if dt is None:
                continue
            if dt <= cutoff:
                baseline = row
            else:
                break

        partial = baseline is None
        if baseline is None:
            baseline = rows[0]

        return current, baseline, partial, True

    @staticmethod
    def _safe_counter_delta(current, baseline, field):
        cur = int(current[field] or 0)
        base = int(baseline[field] or 0)
        return cur - base if cur >= base else cur

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        try:
            return column in {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            return False

    @staticmethod
    def _row_value(row: sqlite3.Row | None, key: str, default: Any = 0) -> Any:
        if row is None:
            return default
        try:
            return row[key] if key in row.keys() else default
        except Exception:
            return default

    @staticmethod
    def _clean_stage_action(value: Any) -> str:
        text = str(value or "")
        text = re.sub(r"(?i)<br\s*/?>", " · ", text)
        text = re.sub(r"<[^>]+>", "", text)
        return " ".join(text.split())

    @classmethod
    def _normalize_stage_log(cls, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
        if not isinstance(raw, list):
            return []
        stages = []
        failure_words = re.compile(
            r"(?i)\b(fail(?:ed|ure)?|error|not enough|cannot|can't|aborted|password|crc|corrupt|missing)\b"
        )
        for stage in raw:
            if not isinstance(stage, dict):
                continue
            name = str(stage.get("name") or "Stage").strip() or "Stage"
            actions = stage.get("actions") or []
            if not isinstance(actions, list):
                actions = [actions]
            cleaned = [cls._clean_stage_action(a) for a in actions]
            cleaned = [a for a in cleaned if a]
            failed = any(failure_words.search(a) for a in cleaned)
            stages.append({
                "name": name,
                "actions": cleaned,
                "status": "failed" if failed else "ok",
            })
        return stages

    @staticmethod
    def _infer_failure_stage(fail_message: str, stages: list[dict[str, Any]]) -> str | None:
        for stage in stages:
            if stage.get("status") == "failed":
                return str(stage.get("name") or "Post-processing")
        msg = str(fail_message or "").lower()
        if not msg:
            return None
        if any(k in msg for k in ("repair", "par2", "par verify", "blocks short", "repair blocks")):
            return "Repair / verification"
        if any(k in msg for k in ("unpack", "unrar", "7zip", "rar", "password")):
            return "Unpack"
        if any(k in msg for k in ("article", "download", "cannot be completed", "not complete", "missing")):
            return "Download"
        if "script" in msg:
            return "Script"
        return "Post-processing"

    def _search_coverage(self, conn: sqlite3.Connection, cutoff: datetime) -> dict[str, Any]:
        """Measure raw indexer availability before Arr acceptance/rejection decisions.

        The numerator is intentionally simple: for a search whose active Arr quality
        target was snapshotted, did a queried indexer return at least one release for
        the correct media whose Arr-parsed quality belongs to that target? Custom
        format score, size, language, release group, retention, SAB outcome and import
        outcome do not affect this metric.

        Quality-profile snapshots are time-sensitive. If no profile snapshot was
        captured for an historical search, the target remains unresolved rather than
        applying today's mutable profile retroactively.
        """
        tiers = ["2160p", "1080p", "720p", "SD"]
        empty = {
            "sessions": [], "indexers": [], "global_target_misses": [],
            "global_wanted_gaps": [], "quality_tiers": tiers,
            "target_known_sessions": 0, "unresolved_sessions": 0,
        }
        if not self._table_exists(conn, "arr_search_candidates"):
            return empty

        have_prowlarr = (
            self._table_exists(conn, "prowlarr_log_searches")
            and self._column_exists(conn, "prowlarr_log_searches", "result_count")
        )
        have_targets = self._table_exists(conn, "arr_search_targets")
        have_quality_name = self._column_exists(conn, "arr_search_candidates", "quality_name")
        have_media_match = self._column_exists(conn, "arr_search_candidates", "media_match")

        # Raw Search Results is a Usenet/NZB analytics view. Historical Prowlarr
        # logs can contain torrent indexers too, so constrain this layer to the
        # indexers Prowlarr currently identifies as Usenet/Newznab. The main
        # Indexers page already follows the same protocol boundary.
        usenet_indexer_names: set[str] | None = None
        if self._table_exists(conn, "prowlarr_indexers") and self._column_exists(conn, "prowlarr_indexers", "protocol"):
            rows = conn.execute(
                """SELECT name FROM prowlarr_indexers
                   WHERE lower(trim(coalesce(protocol,''))) IN ('usenet','nzb','newznab')"""
            ).fetchall()
            if rows:
                usenet_indexer_names = {str(r["name"] or "").strip().lower() for r in rows if str(r["name"] or "").strip()}

        cutoff_sql = cutoff.isoformat(timespec="seconds")
        prow_cutoff_sql = (cutoff - timedelta(seconds=2)).isoformat(timespec="seconds")

        search_rows = conn.execute(
            """SELECT event_id,event_time,app,search_mode,origin_confidence,media_title,season,episode,details_json
               FROM arr_events
               WHERE event_type='search_started' AND event_time >= ?
               ORDER BY event_time""",
            (cutoff_sql,),
        ).fetchall()

        candidate_cols = [
            "candidate_id", "search_event_id", "event_time", "app", "media_title", "season", "episode",
            "indexer", "release_name", "quality_raw", "quality_tier", "decision", "rejection_reasons",
        ]
        if have_quality_name:
            candidate_cols.append("quality_name")
        if have_media_match:
            candidate_cols.append("media_match")
        # Candidate rows are linked to the logical search id. Restricting them by
        # the selected search window avoids reparsing historical candidates on every
        # tab navigation while preserving the exact same per-search semantics.
        candidates = conn.execute(
            f"""SELECT {','.join('c.' + c for c in candidate_cols)}
                FROM arr_search_candidates c
                JOIN arr_events a ON a.event_id=c.search_event_id
                WHERE a.event_type='search_started' AND a.event_time >= ?
                ORDER BY c.event_time""",
            (cutoff_sql,),
        ).fetchall()

        targets_by_search: dict[str, sqlite3.Row] = {}
        if have_targets:
            for row in conn.execute(
                """SELECT t.search_event_id,t.event_time,t.app,t.profile_id,t.profile_name,t.target_label,
                          t.target_qualities_json,t.target_tiers_json,t.source,t.confidence,t.captured_at,t.details_json
                   FROM arr_search_targets t
                   JOIN arr_events a ON a.event_id=t.search_event_id
                   WHERE a.event_type='search_started' AND a.event_time >= ?
                   ORDER BY t.event_time""",
                (cutoff_sql,),
            ).fetchall():
                targets_by_search[str(row["search_event_id"])] = row

        prow_parsed: list[tuple[datetime, sqlite3.Row]] = []
        if have_prowlarr:
            for row in conn.execute(
                """SELECT event_id,event_time,indexer_name,query_text,query_signature,result_count,raw_line
                   FROM prowlarr_log_searches
                   WHERE event_time >= ?
                   ORDER BY event_time""",
                (prow_cutoff_sql,),
            ).fetchall():
                dt = self._local_dt(row["event_time"])
                if dt is not None:
                    prow_parsed.append((dt, row))
        prow_times = [item[0] for item in prow_parsed]

        cand_by_search: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in candidates:
            cand_by_search[str(row["search_event_id"])].append(row)

        def safe_json_list(value: Any) -> list[str]:
            try:
                raw = json.loads(value or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
            return [str(x).strip() for x in raw if str(x).strip()] if isinstance(raw, list) else []

        def quality_key(value: Any) -> str:
            text = re.sub(r"(?i)\s+v\d+\s*$", "", str(value or "").strip())
            return re.sub(r"[^a-z0-9]+", "", text.lower())

        def media_label(row: sqlite3.Row) -> str:
            title = str(row["media_title"] or "Search")
            if str(row["app"] or "").lower() == "sonarr" and row["season"] is not None:
                title += f" · S{int(row['season']):02d}"
                if row["episode"] is not None:
                    title += f"E{int(row['episode']):02d}"
            return title

        def scope_label(row: sqlite3.Row) -> str:
            if str(row["app"] or "").lower() == "radarr":
                return "Movie"
            if row["season"] is not None and row["episode"] is not None:
                return "Episode"
            if row["season"] is not None:
                return "Season"
            return "Series"

        def query_matches_session(qrow: sqlite3.Row, srow: sqlite3.Row) -> bool:
            raw = str(qrow["raw_line"] or "")
            if str(srow["app"] or "").lower() == "sonarr" and srow["season"] is not None:
                season = int(srow["season"])
                episode = srow["episode"]
                if episode is not None:
                    return f"s{season:02d}e{int(episode):02d}" in raw.lower()
                return f"s{season:02d}" in raw.lower()
            return True

        def candidate_is_media_match(c: sqlite3.Row) -> bool:
            if not have_media_match:
                return True
            # Unknown parser identity remains usable evidence; only an explicit
            # mismatch is discarded (e.g. Hill Street returned for 21 Jump Street).
            return c["media_match"] is None or int(c["media_match"] or 0) != 0

        indexer_acc: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "searches": 0,
            "target_opportunities": 0,
            "target_hits": 0,
            "matching_results": 0,
            "raw_candidates": 0,
            "selected": 0,
            "target_tier_results": defaultdict(int),
            "raw_tier_results": defaultdict(int),
        })
        sessions: list[dict[str, Any]] = []

        for srow in search_rows:
            sdt = self._local_dt(srow["event_time"])
            if sdt is None or sdt < cutoff:
                continue
            sid = str(srow["event_id"])
            scands_all = cand_by_search.get(sid, [])
            scands = [c for c in scands_all if candidate_is_media_match(c)]

            # Prowlarr fan-out for an Arr search is constrained to a tiny
            # [-2s,+45s] interval. Binary-search directly into the ordered query
            # timeline instead of rescanning the full retained Prowlarr list for
            # every logical search (the old O(searches * queries) hot path).
            qrows: list[sqlite3.Row] = []
            q_start = bisect_left(prow_times, sdt - timedelta(seconds=2))
            q_end = bisect_right(prow_times, sdt + timedelta(seconds=45))
            for _qdt, qrow in prow_parsed[q_start:q_end]:
                if query_matches_session(qrow, srow):
                    qrows.append(qrow)

            names = {str(c["indexer"] or "").strip() for c in scands if str(c["indexer"] or "").strip()}
            names.update(str(r["indexer_name"] or "").strip() for r in qrows if str(r["indexer_name"] or "").strip())
            if usenet_indexer_names is not None:
                names = {name for name in names if name.lower() in usenet_indexer_names}
                scands = [c for c in scands if str(c["indexer"] or "").strip().lower() in usenet_indexer_names]
                qrows = [r for r in qrows if str(r["indexer_name"] or "").strip().lower() in usenet_indexer_names]
            if not names:
                continue

            target = targets_by_search.get(sid)
            target_qualities = safe_json_list(target["target_qualities_json"]) if target else []
            target_tiers = safe_json_list(target["target_tiers_json"]) if target else []
            target_keys = {quality_key(x) for x in target_qualities if quality_key(x)}
            target_known = bool(target and target_keys)
            target_label = str(target["target_label"] or "").strip() if target else None

            per: dict[str, dict[str, Any]] = {}
            for name in sorted(names, key=str.lower):
                cs = [c for c in scands if str(c["indexer"] or "").strip().lower() == name.lower()]
                qs = [r for r in qrows if str(r["indexer_name"] or "").strip().lower() == name.lower()]

                # Candidate rows may appear through multiple Prowlarr query strategies.
                # Count each release once within this logical Arr search/indexer.
                unique: dict[str, sqlite3.Row] = {}
                for c in cs:
                    rel = re.sub(r"\s+", " ", str(c["release_name"] or "").strip()).lower()
                    key = rel or str(c["candidate_id"])
                    unique.setdefault(key, c)
                cs = list(unique.values())

                raw_quality_counts: dict[str, int] = defaultdict(int)
                raw_tier_counts = {tier: 0 for tier in tiers}
                matching_quality_counts: dict[str, int] = defaultdict(int)
                matching_tier_counts = {tier: 0 for tier in tiers}
                target_matches: list[sqlite3.Row] = []
                for c in cs:
                    qname = str(c["quality_name"] or "").strip() if have_quality_name else str(c["quality_raw"] or "").strip()
                    tier = str(c["quality_tier"] or "").strip()
                    if qname:
                        raw_quality_counts[qname] += 1
                    if tier in raw_tier_counts:
                        raw_tier_counts[tier] += 1
                    if target_known and quality_key(qname) in target_keys:
                        target_matches.append(c)
                        if qname:
                            matching_quality_counts[qname] += 1
                        if tier in matching_tier_counts:
                            matching_tier_counts[tier] += 1

                per[name] = {
                    "indexer": name,
                    "query_events": len(qs),
                    "raw_count": len(cs),
                    "target_count": len(target_matches),
                    "target_hit": bool(target_matches) if target_known else None,
                    "raw_quality_counts": dict(sorted(raw_quality_counts.items())),
                    "matching_quality_counts": dict(sorted(matching_quality_counts.items())),
                    "raw_tier_counts": raw_tier_counts,
                    "matching_tier_counts": matching_tier_counts,
                    "selected_count": sum(str(c["decision"] or "") == "selected" for c in cs),
                    # Diagnostic only. It is not used in raw availability scoring.
                    "prowlarr_report_max": max(
                        [int(r["result_count"]) for r in qs if r["result_count"] is not None],
                        default=None,
                    ),
                }

            target_indexers = [name for name, item in per.items() if int(item["target_count"]) > 0] if target_known else []
            selected_rows = [c for c in scands if str(c["decision"] or "") == "selected"]
            if selected_rows:
                sel = selected_rows[0]
                sel_quality = str(sel["quality_name"] or sel["quality_tier"] or "quality unknown") if have_quality_name else str(sel["quality_tier"] or "quality unknown")
                grab_outcome = f"grabbed {sel_quality} via {str(sel['indexer'] or 'indexer')}"
                grab_outcome_kind = "grabbed"
            elif any(int(item["raw_count"]) > 0 for item in per.values()):
                grab_outcome = "no grab"
                grab_outcome_kind = "no_grab"
            else:
                grab_outcome = "no candidates returned"
                grab_outcome_kind = "no_candidates"

            # Even when the positive target cannot be reconstructed safely,
            # retained Arr decisions can still tell us which observed qualities
            # were explicitly outside the profile. Expose that as negative
            # evidence without using it to score availability.
            rejected_profile_qualities: set[str] = set()
            rejected_profile_tiers: set[str] = set()
            if not target_known:
                for c in scands:
                    reasons = str(c["rejection_reasons"] or "").lower()
                    if "not wanted in profile" not in reasons:
                        continue
                    qname = str(c["quality_name"] or "").strip() if have_quality_name else str(c["quality_raw"] or "").strip()
                    tier = str(c["quality_tier"] or "").strip()
                    if qname:
                        rejected_profile_qualities.add(qname)
                    if tier in tiers:
                        rejected_profile_tiers.add(tier)

            if not target_known:
                classification = "target_unresolved"
            elif not target_indexers:
                classification = "global_target_miss"
            elif len(target_indexers) < len(per):
                classification = "partial_target_coverage"
            else:
                classification = "target_covered"

            for name, item in per.items():
                acc = indexer_acc[name]
                acc["searches"] += 1
                acc["raw_candidates"] += int(item["raw_count"])
                acc["selected"] += int(item["selected_count"])
                for tier, n in item["raw_tier_counts"].items():
                    acc["raw_tier_results"][tier] += int(n)
                if target_known:
                    acc["target_opportunities"] += 1
                    acc["matching_results"] += int(item["target_count"])
                    if int(item["target_count"]) > 0:
                        acc["target_hits"] += 1
                    for tier, n in item["matching_tier_counts"].items():
                        acc["target_tier_results"][tier] += int(n)

            sessions.append({
                "search_id": sid,
                "time": str(srow["event_time"]),
                "app": str(srow["app"] or "").title(),
                "origin": str(srow["search_mode"] or "unknown"),
                "origin_confidence": str(srow["origin_confidence"] or "none"),
                "media": media_label(srow),
                "media_title": str(srow["media_title"] or "Search"),
                "season": srow["season"], "episode": srow["episode"],
                "scope": scope_label(srow),
                "indexers": list(per.values()),
                "queried": len(per),
                "candidate_count": sum(int(x["raw_count"]) for x in per.values()),
                "target_known": target_known,
                "target_label": target_label,
                "target_qualities": target_qualities,
                "target_tiers": target_tiers,
                "target_source": str(target["source"] or "") if target else None,
                "target_confidence": str(target["confidence"] or "") if target else None,
                "target_captured_at": str(target["captured_at"] or "") if target else None,
                "rejected_profile_qualities": sorted(rejected_profile_qualities),
                "rejected_profile_tiers": [tier for tier in tiers if tier in rejected_profile_tiers],
                "target_indexers": target_indexers,
                "global_target_miss": bool(target_known and not target_indexers),
                # Backward-compatible name used by some older page links/caches.
                "global_wanted_gap": bool(target_known and not target_indexers),
                "classification": classification,
                "grab_outcome": grab_outcome,
                "grab_outcome_kind": grab_outcome_kind,
                "end_result": grab_outcome,
                "end_result_kind": grab_outcome_kind,
            })

        indexers: list[dict[str, Any]] = []
        for name, acc in sorted(indexer_acc.items(), key=lambda kv: kv[0].lower()):
            opps = int(acc["target_opportunities"])
            hits = int(acc["target_hits"])
            coverage = pct(hits, opps) if opps else None
            row = {
                "indexer": name,
                "searches": int(acc["searches"]),
                "opportunities": int(acc["searches"]),
                "target_opportunities": opps,
                "target_hits": hits,
                "target_misses": opps - hits,
                "availability_score": coverage,
                "target_coverage": coverage,
                "matching_results": int(acc["matching_results"]),
                "raw_candidates": int(acc["raw_candidates"]),
                "selected": int(acc["selected"]),
                "target_tiers": {tier: {"results": int(acc["target_tier_results"][tier])} for tier in tiers},
                "raw_tiers": {tier: {"results": int(acc["raw_tier_results"][tier])} for tier in tiers},
                # Compatibility aliases for snapshots created before the templates
                # were refreshed. These aliases carry the new unblended semantics.
                "wanted_opportunities": opps,
                "wanted_hits": hits,
                "wanted_misses": opps - hits,
                "wanted_coverage": coverage,
                "discovery_score": coverage,
                "result_count_total": int(acc["raw_candidates"]),
                "tiers": {tier: {"hits": int(acc["target_tier_results"][tier])} for tier in tiers},
            }
            indexers.append(row)

        sessions.sort(key=lambda x: x["time"], reverse=True)
        target_known_sessions = sum(bool(x["target_known"]) for x in sessions)
        unresolved_sessions = len(sessions) - target_known_sessions
        global_misses = [x for x in sessions if x["global_target_miss"]]
        return {
            "sessions": sessions,
            "indexers": indexers,
            "global_target_misses": global_misses,
            "global_wanted_gaps": global_misses,
            "quality_tiers": tiers,
            "target_known_sessions": target_known_sessions,
            "unresolved_sessions": unresolved_sessions,
        }
    def snapshot(self, hours: int = 168) -> dict[str, Any]:
        hours = max(1, min(int(hours), 24 * 60))

        with self.connect() as conn:
            exact_grabs, episode_grabs = self._grab_indexes(conn)

            now = local_now()
            activity_cutoff = now - timedelta(hours=hours)
            attribution_hours = self.attribution_hours

            # Shared chart bucket sizing. All dashboard/provider/indexer trend
            # series use the selected activity window and the same time buckets,
            # so changing the window redraws every chart from the same evidence
            # slice without hard-coded provider/indexer names.
            if hours <= 24:
                trend_bucket_hours = 2
            elif hours <= 72:
                trend_bucket_hours = 6
            elif hours <= 168:
                trend_bucket_hours = 12
            elif hours <= 336:
                trend_bucket_hours = 24
            elif hours <= 720:
                trend_bucket_hours = 48
            else:
                trend_bucket_hours = 96
            trend_step = timedelta(hours=trend_bucket_hours)
            trend_step_seconds = trend_step.total_seconds()

            def trend_bucket(dt: datetime) -> datetime:
                offset = max(0.0, (dt - activity_cutoff).total_seconds())
                bucket_num = int(offset // trend_step_seconds)
                return activity_cutoff + (trend_step * bucket_num)

            # ---------------------------------------------------------------
            # Providers: SAB article availability + observed contribution
            # ---------------------------------------------------------------
            providers = []
            provider_series = []

            provider_names = [
                r["provider"]
                for r in conn.execute(
                    "SELECT DISTINCT provider FROM provider_snapshots ORDER BY provider"
                ).fetchall()
            ]

            # SAB History exposes a structured ``Servers`` stage for many jobs,
            # e.g. ``provider-a.example=7.2 GB, provider-b.example=13.3 GB``.
            # Aggregate that retained evidence separately from server_stats so we
            # can show contribution without pretending a shared job belongs to one
            # provider.  Values are approximate because SAB renders the stage in
            # human-readable rounded units.
            contribution_by_provider: dict[str, dict[str, Any]] = defaultdict(lambda: {
                "bytes": 0, "jobs": 0, "completed_jobs": 0, "exclusive_jobs": 0,
            })
            contribution_time_buckets: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            contribution_time_jobs: dict[datetime, int] = defaultdict(int)
            contribution_jobs = 0
            contribution_completed_jobs = 0
            contribution_shared_jobs = 0
            contribution_total_bytes = 0
            contribution_rows = []
            if self._table_exists(conn, "sab_history"):
                contribution_rows = conn.execute(
                    """SELECT history_id,completed_time,job_name,status,stage_log_json
                       FROM sab_history
                       WHERE COALESCE(completed_time,'') >= ?
                       ORDER BY completed_time""",
                    (activity_cutoff.isoformat(timespec="milliseconds"),),
                ).fetchall()

            for h in contribution_rows:
                usage = extract_server_contributions(h["stage_log_json"])
                usage = {k: int(v) for k, v in usage.items() if int(v or 0) > 0}
                if not usage:
                    continue
                contribution_jobs += 1
                is_completed = str(h["status"] or "").lower() == "completed"
                contribution_completed_jobs += int(is_completed)
                if len(usage) > 1:
                    contribution_shared_jobs += 1
                hdt = self._local_dt(h["completed_time"])
                hbucket = trend_bucket(hdt) if hdt is not None else None
                if hbucket is not None:
                    contribution_time_jobs[hbucket] += 1
                for server, used_bytes in usage.items():
                    key = str(server).strip().lower()
                    bucket = contribution_by_provider[key]
                    bucket["bytes"] += used_bytes
                    bucket["jobs"] += 1
                    bucket["completed_jobs"] += int(is_completed)
                    bucket["exclusive_jobs"] += int(len(usage) == 1)
                    contribution_total_bytes += used_bytes
                    if hbucket is not None:
                        contribution_time_buckets[hbucket][key] += used_bytes

            provider_contribution_series = []
            for bucket_time, by_provider in sorted(contribution_time_buckets.items()):
                bucket_total = sum(int(v or 0) for v in by_provider.values())
                if bucket_total <= 0:
                    continue
                for provider_name, used_bytes in sorted(by_provider.items()):
                    provider_contribution_series.append({
                        "time": bucket_time.isoformat(timespec="seconds"),
                        "provider": provider_name,
                        "provider_label": provider_label(provider_name),
                        "bytes": int(used_bytes),
                        "bytes_human": human_bytes(used_bytes),
                        "share": pct(used_bytes, bucket_total, 1),
                        "jobs": int(contribution_time_jobs.get(bucket_time, 0)),
                    })

            provider_display_labels: dict[str, str] = {}
            for provider_name in provider_names:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM provider_snapshots
                    WHERE provider=?
                    ORDER BY id
                    """,
                    (provider_name,),
                ).fetchall()
                if not rows:
                    continue

                current, baseline, partial, in_window = self._window_baseline(
                    rows, activity_cutoff
                )

                tried = int(current["articles_tried"] or 0)
                unavailable = int(current["articles_success"] or 0)
                available = max(0, tried - unavailable)

                if in_window:
                    w_tried = self._safe_counter_delta(
                        current, baseline, "articles_tried"
                    )
                    w_unavailable = self._safe_counter_delta(
                        current, baseline, "articles_success"
                    )
                    w_available = max(0, w_tried - w_unavailable)
                    w_bytes = self._safe_counter_delta(
                        current, baseline, "total_bytes"
                    )
                else:
                    w_tried = w_unavailable = w_available = w_bytes = 0

                baseline_dt = self._local_dt(baseline["collected_at"]) if baseline else None
                current_dt = self._local_dt(current["collected_at"])
                coverage_hours = None
                if baseline_dt is not None and current_dt is not None:
                    coverage_hours = round(
                        max(0.0, (current_dt - baseline_dt).total_seconds() / 3600.0),
                        1,
                    )

                pkey = str(provider_name).strip().lower()
                configured_label = str(self._row_value(current, "provider_label", "") or "").strip()
                # Older snapshots sometimes stored the hostname itself as the
                # label. Prefer an actual configured display/name value; if the
                # stored value still looks like a host, derive a readable label
                # without a provider-name registry.
                looks_like_host = (not configured_label or "." in configured_label or configured_label.lower() == pkey)
                display_label = provider_label(provider_name) if looks_like_host else configured_label
                provider_display_labels[pkey] = display_label
                contrib = contribution_by_provider.get(pkey, {})
                contrib_bytes = int(contrib.get("bytes", 0) or 0)
                priority = self._row_value(current, "priority", None)
                connections = self._row_value(current, "connections", None)
                enabled = self._row_value(current, "enabled", None)

                providers.append({
                    "provider": provider_name,
                    "provider_label": display_label,

                    # Current cumulative/raw SAB counters.  SAB's UI expresses
                    # article availability as (tried - articles_success) / tried.
                    # Keep the raw API field visible as unavailable_counter so the
                    # display agrees with Config -> Servers without calling it a
                    # release completion percentage.
                    "tried": tried,
                    "unavailable_counter": unavailable,
                    "available_counter": available,
                    "availability": pct(available, tried, 3),
                    "unavailable_rate": pct(unavailable, tried, 3),
                    # Compatibility aliases for older templates/plugins.
                    "secondary": unavailable,
                    "difference": available,
                    "counter_ratio": pct(unavailable, tried, 3),
                    "today": human_bytes(current["day_bytes"]),
                    "week": human_bytes(current["week_bytes"]),
                    "month": human_bytes(current["month_bytes"]),
                    "total": human_bytes(current["total_bytes"]),
                    "collected_at": current["collected_at"],
                    "priority": int(priority) if priority not in (None, "") else None,
                    "connections": int(connections) if connections not in (None, "") else None,
                    "enabled": bool(enabled) if enabled not in (None, "") else None,

                    # Selected-window deltas from stored server_stats snapshots.
                    "window_tried": w_tried,
                    "window_unavailable": w_unavailable,
                    "window_available": w_available,
                    "window_availability": pct(w_available, w_tried, 3),
                    "window_unavailable_rate": pct(w_unavailable, w_tried, 3),
                    "window_secondary": w_unavailable,
                    "window_difference": w_available,
                    "window_ratio": pct(w_unavailable, w_tried, 3),
                    "window_bytes": w_bytes,
                    "window_bytes_human": human_bytes(w_bytes),
                    "window_baseline_at": baseline["collected_at"] if baseline else None,
                    "window_coverage_hours": coverage_hours,
                    "window_partial": partial,
                    "window_has_current_snapshot": in_window,

                    # Per-job contribution reconstructed from SAB History's Servers
                    # stage. This is intentionally separate from availability.
                    "contribution_bytes": contrib_bytes,
                    "contribution_bytes_human": human_bytes(contrib_bytes),
                    "contribution_share": pct(contrib_bytes, contribution_total_bytes, 1),
                    "contribution_jobs": int(contrib.get("jobs", 0) or 0),
                    "contribution_completed_jobs": int(contrib.get("completed_jobs", 0) or 0),
                    "exclusive_jobs": int(contrib.get("exclusive_jobs", 0) or 0),
                })

                for r in rows:
                    dt = self._local_dt(r["collected_at"])
                    if dt is not None and dt < activity_cutoff:
                        continue
                    r_tried = int(r["articles_tried"] or 0)
                    r_unavailable = int(r["articles_success"] or 0)
                    r_available = max(0, r_tried - r_unavailable)
                    provider_series.append({
                        "time": r["collected_at"],
                        "provider": provider_name,
                        "ratio": pct(r_available, r_tried, 3),
                    })

            for row in provider_contribution_series:
                raw_provider = str(row.get("provider") or "").strip().lower()
                row["provider_label"] = provider_display_labels.get(raw_provider, provider_label(raw_provider))

            provider_contribution = {
                "bytes": contribution_total_bytes,
                "bytes_human": human_bytes(contribution_total_bytes),
                "jobs": contribution_jobs,
                "completed_jobs": contribution_completed_jobs,
                "shared_jobs": contribution_shared_jobs,
                "exclusive_jobs": max(0, contribution_jobs - contribution_shared_jobs),
                "providers": sum(1 for p in providers if p.get("contribution_bytes", 0) > 0),
                "approximate": True,
            }

            # Dashboard provider trend uses the last observed server counter in
            # each shared time bucket. Provider identities are discovered from
            # stored snapshots, never from a fixed list.
            provider_availability_bucketed: dict[tuple[str, datetime], dict[str, Any]] = {}
            for row in provider_series:
                rdt = self._local_dt(row.get("time"))
                if rdt is None or row.get("ratio") is None:
                    continue
                key = (str(row.get("provider") or "Provider"), trend_bucket(rdt))
                provider_availability_bucketed[key] = row
            provider_availability_series = [
                {
                    "time": bucket_time.isoformat(timespec="seconds"),
                    "series": provider_display_labels.get(str(provider_name).strip().lower(), provider_label(provider_name)),
                    "value": float(row["ratio"]),
                    "sample": 1,
                }
                for (provider_name, bucket_time), row in sorted(
                    provider_availability_bucketed.items(), key=lambda kv: (kv[0][1], kv[0][0].lower())
                )
            ]

            # ---------------------------------------------------------------
            # Prowlarr indexers:
            #   - current API totals remain available
            #   - window_* fields are cumulative-counter deltas for the
            #     selected Activity Window
            # ---------------------------------------------------------------
            indexers = []
            indexer_ids = [
                int(r["indexer_id"])
                for r in conn.execute(
                    """
                    SELECT DISTINCT indexer_id
                    FROM prowlarr_stats_snapshots
                    ORDER BY indexer_id
                    """
                ).fetchall()
            ]

            for indexer_id in indexer_ids:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM prowlarr_stats_snapshots
                    WHERE indexer_id=?
                    ORDER BY id
                    """,
                    (indexer_id,),
                ).fetchall()
                if not rows:
                    continue

                current, baseline, partial, in_window = self._window_baseline(
                    rows, activity_cutoff
                )
                keys = set(current.keys())

                all_queries = int(current["queries"] or 0)
                all_grabs = int(current["grabs"] or 0)

                if in_window:
                    window_queries = self._safe_counter_delta(current, baseline, "queries")
                    window_grabs = self._safe_counter_delta(current, baseline, "grabs")
                    window_failed_queries = (
                        self._safe_counter_delta(current, baseline, "failed_queries")
                        if "failed_queries" in keys else 0
                    )
                    window_failed_grabs = (
                        self._safe_counter_delta(current, baseline, "failed_grabs")
                        if "failed_grabs" in keys else 0
                    )
                    window_failed_rss = (
                        self._safe_counter_delta(current, baseline, "failed_rss")
                        if "failed_rss" in keys else 0
                    )
                else:
                    window_queries = window_grabs = 0
                    window_failed_queries = window_failed_grabs = window_failed_rss = 0

                baseline_dt = self._local_dt(baseline["collected_at"]) if baseline else None
                current_dt = self._local_dt(current["collected_at"])
                coverage_hours = None
                if baseline_dt is not None and current_dt is not None:
                    coverage_hours = round(
                        max(0.0, (current_dt - baseline_dt).total_seconds() / 3600.0),
                        1,
                    )

                indexers.append({
                    "indexer_id": indexer_id,
                    "name": current["indexer_name"],

                    # Current cumulative Prowlarr API counters.
                    "queries": all_queries,
                    "grabs": all_grabs,
                    "grab_query": pct(all_grabs, all_queries),
                    "failed_queries": int(current["failed_queries"] or 0)
                        if "failed_queries" in keys else 0,
                    "failed_grabs": int(current["failed_grabs"] or 0)
                        if "failed_grabs" in keys else 0,
                    "failed_rss": int(current["failed_rss"] or 0)
                        if "failed_rss" in keys else 0,
                    "avg_query_ms": round(float(current["avg_response_ms"] or 0), 1),
                    "avg_grab_ms": round(float(current["avg_grab_response_ms"] or 0), 1)
                        if "avg_grab_response_ms" in keys else 0.0,

                    # Selected Activity Window.
                    "window_queries": window_queries,
                    "window_grabs": window_grabs,
                    "window_grab_query": pct(window_grabs, window_queries),
                    "window_failed_queries": window_failed_queries,
                    "window_failed_grabs": window_failed_grabs,
                    "window_failed_rss": window_failed_rss,
                    "window_baseline_at": baseline["collected_at"] if baseline else None,
                    "window_coverage_hours": coverage_hours,
                    "window_partial": partial,
                    "window_has_current_snapshot": in_window,
                })

            # ---------------------------------------------------------------
            # Prowlarr history activity. These are explicitly event counts,
            # not conversion percentages: search, RSS and grab populations
            # are not guaranteed to have a 1:1 relationship.
            # ---------------------------------------------------------------
            history_activity = defaultdict(lambda: {
                "search_events": 0,
                "rss_events": 0,
                "grab_events": 0,
                "grab_methods": defaultdict(int),
                "grab_sources": defaultdict(int),
            })

            history_rows = conn.execute(
                """
                SELECT event_time,indexer_id,indexer_name,event_type,data_json
                FROM prowlarr_history
                WHERE event_time >= ?
                ORDER BY event_time
                """,
                (activity_cutoff.isoformat(timespec="seconds"),),
            ).fetchall()

            for r in history_rows:
                dt = self._local_dt(r["event_time"])
                if dt is not None and dt < activity_cutoff:
                    continue
                name = str(r["indexer_name"] or r["indexer_id"] or "(unknown)")
                event_type = str(r["event_type"] or "").lower()
                bucket = history_activity[name]

                if "grab" in event_type:
                    bucket["grab_events"] += 1
                    try:
                        data = json.loads(r["data_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        data = {}
                    if isinstance(data, dict):
                        method = str(data.get("grabMethod") or "").strip()
                        source = str(data.get("source") or "").strip()
                        if method:
                            bucket["grab_methods"][method] += 1
                        if source:
                            bucket["grab_sources"][source] += 1
                elif "rss" in event_type:
                    bucket["rss_events"] += 1
                elif "query" in event_type:
                    bucket["search_events"] += 1

            for item in indexers:
                h = history_activity.get(item["name"], {})
                item["history_search_events"] = int(h.get("search_events", 0))
                item["history_rss_events"] = int(h.get("rss_events", 0))
                item["history_grab_events"] = int(h.get("grab_events", 0))
                item["grab_methods"] = [
                    {"name": k, "count": v}
                    for k, v in sorted(
                        dict(h.get("grab_methods", {})).items(),
                        key=lambda kv: (-kv[1], kv[0].lower()),
                    )
                ]
                item["grab_sources"] = [
                    {"name": k, "count": v}
                    for k, v in sorted(
                        dict(h.get("grab_sources", {})).items(),
                        key=lambda kv: (-kv[1], kv[0].lower()),
                    )[:5]
                ]

            all_jobs = conn.execute(
                """
                SELECT event_id,event_time,job_name,status,max_repair_blocks
                FROM jobs
                ORDER BY event_time
                """
            ).fetchall()

            attempt_diag_by_event = {}
            if self._table_exists(conn, "sab_attempt_diagnostics"):
                diag_extra = ""
                if self._column_exists(conn, "sab_attempt_diagnostics", "failure_code"):
                    diag_extra += ",failure_code"
                if self._column_exists(conn, "sab_attempt_diagnostics", "failure_confidence"):
                    diag_extra += ",failure_confidence"
                for d in conn.execute(
                    f"""
                    SELECT event_id,failure_stage,failure_reason,lifecycle_json,source,
                           history_id,log_event_count{diag_extra}
                    FROM sab_attempt_diagnostics
                    """
                ).fetchall():
                    try:
                        lifecycle = json.loads(d["lifecycle_json"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        lifecycle = []
                    attempt_diag_by_event[d["event_id"]] = {
                        "failure_stage": d["failure_stage"],
                        "failure_code": self._row_value(d, "failure_code", None),
                        "failure_confidence": self._row_value(d, "failure_confidence", None),
                        "failure_reason": d["failure_reason"],
                        "lifecycle": lifecycle if isinstance(lifecycle, list) else [],
                        "source": d["source"] or "inferred",
                        "history_id": d["history_id"],
                        "log_event_count": int(d["log_event_count"] or 0),
                    }

            def effective_job_status(row) -> str:
                raw = str(row["status"] or "").lower()
                diag = attempt_diag_by_event.get(row["event_id"], {})
                if raw in {"cancelled", "canceled", "aborted"}:
                    return "cancelled"
                if raw == "failed" and str(diag.get("failure_code") or "").lower() == "cancelled_aborted":
                    return "cancelled"
                return raw

            def in_activity_window(event_time: str) -> bool:
                dt = parse_dt(event_time)
                if dt is None:
                    return True
                if dt.tzinfo is None:
                    dt = localize(dt)
                return dt >= activity_cutoff

            # Build full-history exact-release groups, then split repeated downloads of
            # the same NZB/title into acquisition-sized clusters.  A release downloaded
            # again days later is a new acquisition, not another retry of the old one.
            all_grouped = defaultdict(list)
            for r in all_jobs:
                all_grouped[normalize_identity(r["job_name"])].append(r)

            def split_release_clusters(items, gap=timedelta(hours=6)):
                ordered = sorted(items, key=lambda row: str(row["event_time"] or ""))
                clusters = []
                current = []
                previous_dt = None
                for row in ordered:
                    row_dt = self._local_dt(row["event_time"])
                    if current and previous_dt is not None and row_dt is not None and row_dt - previous_dt > gap:
                        clusters.append(current)
                        current = []
                    current.append(row)
                    if row_dt is not None:
                        previous_dt = row_dt
                if current:
                    clusters.append(current)
                return clusters

            release_clusters = []
            for items in all_grouped.values():
                release_clusters.extend(split_release_clusters(items))

            release_meta_by_event = {}
            for items in release_clusters:
                statuses = [effective_job_status(x) for x in items]
                failed_seen = False
                recovered = False
                for x in items:
                    st = effective_job_status(x)
                    if st == "failed":
                        failed_seen = True
                    elif st == "completed" and failed_seen:
                        recovered = True
                        break

                eventually_completed = "completed" in statuses
                has_failed = "failed" in statuses
                cancelled_only = bool(statuses) and not eventually_completed and not has_failed and "cancelled" in statuses
                meta = {
                    "recovered": recovered,
                    "eventually_completed": eventually_completed,
                    "outcome": (
                        "recovered" if recovered
                        else "completed" if eventually_completed
                        else "failed" if has_failed
                        else "cancelled" if cancelled_only
                        else "unresolved"
                    ),
                }
                for row in items:
                    release_meta_by_event[str(row["event_id"])] = meta

            window_jobs = [r for r in all_jobs if in_activity_window(str(r["event_time"]))]

            # Attempt-level attribution is windowed by attempt time.
            attempts = []
            attempt_by = defaultdict(lambda: {
                "attempts": 0, "completed": 0, "failed": 0,
                "repair": 0, "heavy": 0,
                "exact": 0, "episode": 0
            })
            attempt_conf = {"exact": 0, "episode": 0, "unattributed": 0}

            for r in window_jobs:
                grab, method = self._attribute(
                    exact_grabs, episode_grabs,
                    str(r["job_name"]), str(r["event_time"]), attribution_hours
                )
                attempt_conf[method] += 1
                blocks = int(r["max_repair_blocks"] or 0)
                meta = release_meta_by_event.get(
                    str(r["event_id"]),
                    {"outcome": str(r["status"]).lower(), "recovered": False},
                )
                diag = attempt_diag_by_event.get(r["event_id"], {})
                raw_status = str(r["status"]).lower()
                effective_status = effective_job_status(r)
                item = {
                    "event_id": r["event_id"],
                    "time": r["event_time"],
                    "title": r["job_name"],
                    "raw_status": raw_status,
                    "status": effective_status,
                    "release_outcome": meta["outcome"],  # final release-level result
                    "release_recovered": bool(meta["recovered"]),
                    "repair_blocks": blocks,
                    "indexer": grab["indexer"] if grab else "(unattributed)",
                    "match_method": method,
                    "failure_stage": diag.get("failure_stage"),
                    "failure_code": diag.get("failure_code"),
                    "failure_confidence": diag.get("failure_confidence"),
                    "failure_reason": diag.get("failure_reason"),
                    "diagnostic_source": diag.get("source"),
                    "lifecycle": diag.get("lifecycle", []),
                }
                attempts.append(item)

                if grab:
                    d = attempt_by[grab["indexer"]]
                    if item["status"] in {"completed", "failed"}:
                        d["attempts"] += 1
                        d[method] += 1
                        d["completed"] += item["status"] == "completed"
                        d["failed"] += item["status"] == "failed"
                        d["repair"] += blocks > 0
                        d["heavy"] += blocks >= 10
                    elif item["status"] == "cancelled":
                        d["cancelled"] = int(d.get("cancelled", 0)) + 1

            self._attach_arr_media_to_attempts(conn, attempts)
            self._attach_sonarr_season_batches(conn, attempts)

            attempt_stats = []
            for name, v in sorted(attempt_by.items()):
                attempt_stats.append({
                    "indexer": name,
                    **v,
                    # A successful grab is a grab whose own SAB attempt reached
                    # a completed terminal state. A failed attempt that is later
                    # rescued by another indexer never receives this credit.
                    "successful_grabs": int(v["completed"]),
                    "success": pct(v["completed"], v["attempts"]),
                    "repair_pct": pct(v["repair"], v["attempts"]),
                    "heavy_pct": pct(v["heavy"], v["attempts"]),
                })

            # Unique releases are windowed by their final observed event.
            releases = []
            release_by = defaultdict(lambda: {
                "releases": 0, "completed": 0, "failed": 0,
                "recovered": 0, "repair": 0, "heavy": 0,
                "exact": 0, "episode": 0
            })
            release_conf = {"exact": 0, "episode": 0, "unattributed": 0}
            recoveries = []

            for items in release_clusters:
                final = items[-1]
                if not in_activity_window(str(final["event_time"])):
                    continue

                grab, method = self._attribute(
                    exact_grabs, episode_grabs,
                    str(final["job_name"]), str(final["event_time"]), attribution_hours
                )
                release_conf[method] += 1

                statuses = [effective_job_status(x) for x in items]
                max_blocks = max(int(x["max_repair_blocks"] or 0) for x in items)

                failed_seen = False
                recovered = False
                first_failed_grab = None
                first_failed_method = None
                recovered_grab = None
                recovered_method = None

                for x in items:
                    st = effective_job_status(x)
                    xgrab, xmethod = self._attribute(
                        exact_grabs, episode_grabs,
                        str(x["job_name"]), str(x["event_time"]), attribution_hours
                    )
                    if st == "failed":
                        failed_seen = True
                        if first_failed_grab is None:
                            first_failed_grab = xgrab
                            first_failed_method = xmethod
                    elif st == "completed" and failed_seen:
                        recovered = True
                        recovered_grab = xgrab
                        recovered_method = xmethod
                        break

                eventually_completed = "completed" in statuses
                has_failed = "failed" in statuses
                cancelled_only = bool(statuses) and not eventually_completed and not has_failed and "cancelled" in statuses
                outcome = (
                    "recovered" if recovered
                    else "completed" if eventually_completed
                    else "failed" if has_failed
                    else "cancelled" if cancelled_only
                    else "unresolved"
                )

                if recovered:
                    recoveries.append({
                        "title": final["job_name"],
                        "final_time": final["event_time"],
                        "failed_via": first_failed_grab["indexer"] if first_failed_grab else "(unattributed)",
                        "recovered_via": recovered_grab["indexer"] if recovered_grab else "(unattributed)",
                        "failed_match": first_failed_method or "unattributed",
                        "recovered_match": recovered_method or "unattributed",
                        "repair_blocks": max_blocks,
                        "kind": (
                            "cross-indexer"
                            if first_failed_grab and recovered_grab
                            and first_failed_grab["indexer"] != recovered_grab["indexer"]
                            else "same-indexer"
                        ),
                    })

                final_status = effective_job_status(final)
                final_diag = attempt_diag_by_event.get(final["event_id"], {})
                release_item = {
                    "title": final["job_name"],
                    "final_time": final["event_time"],
                    "attempts": len(items),
                    "retry_count": max(0, len(items) - 1),
                    "has_retry": len(items) > 1,
                    "final_status": final_status,
                    "eventually_completed": eventually_completed,
                    "recovered": recovered,
                    "outcome": outcome,
                    "repair_blocks": max_blocks,
                    "indexer": grab["indexer"] if grab else "(unattributed)",
                    "match_method": method,
                    "failure_stage": final_diag.get("failure_stage"),
                    "failure_code": final_diag.get("failure_code"),
                    "failure_confidence": final_diag.get("failure_confidence"),
                    "failure_reason": final_diag.get("failure_reason"),
                    "diagnostic_source": final_diag.get("source"),
                }
                releases.append(release_item)

                if grab:
                    d = release_by[grab["indexer"]]
                    if outcome in {"completed", "recovered", "failed"}:
                        d["releases"] += 1
                        d[method] += 1
                        d["completed"] += eventually_completed
                        d["failed"] += outcome == "failed"
                        d["recovered"] += recovered
                        d["repair"] += max_blocks > 0
                        d["heavy"] += max_blocks >= 10
                    elif outcome == "cancelled":
                        d["cancelled"] = int(d.get("cancelled", 0)) + 1

            release_stats = []
            for name, v in sorted(release_by.items()):
                release_stats.append({
                    "indexer": name,
                    **v,
                    "success": pct(v["completed"], v["releases"]),
                    "repair_pct": pct(v["repair"], v["releases"]),
                    "heavy_pct": pct(v["heavy"], v["releases"]),
                })

            # ---------------------------------------------------------------
            # Previous-window comparison: the immediately preceding interval
            # of equal length, [now - 2*hours, now - hours).
            # ---------------------------------------------------------------
            previous_start = activity_cutoff - timedelta(hours=hours)
            previous_end = activity_cutoff

            def in_previous_window(event_time: str) -> bool:
                dt = parse_dt(event_time)
                if dt is None:
                    return False
                if dt.tzinfo is None:
                    dt = localize(dt)
                return previous_start <= dt < previous_end

            previous_attempts_all = [
                r for r in all_jobs if in_previous_window(str(r["event_time"]))
            ]
            previous_attempts = [r for r in previous_attempts_all if effective_job_status(r) in {"completed", "failed"}]
            previous_completed_attempts = sum(
                effective_job_status(r) == "completed" for r in previous_attempts
            )

            previous_release_by = defaultdict(lambda: {
                "releases": 0, "completed": 0, "failed": 0,
                "repair": 0, "heavy": 0,
            })
            previous_release_total = 0
            previous_release_completed = 0

            for items in release_clusters:
                final = items[-1]
                if not in_previous_window(str(final["event_time"])):
                    continue

                statuses = [effective_job_status(x) for x in items]
                completed = "completed" in statuses
                has_failed = "failed" in statuses
                if not completed and not has_failed:
                    continue
                previous_release_total += 1
                previous_release_completed += int(completed)
                max_blocks = max(int(x["max_repair_blocks"] or 0) for x in items)

                grab, _method = self._attribute(
                    exact_grabs, episode_grabs,
                    str(final["job_name"]), str(final["event_time"]), attribution_hours
                )
                if grab:
                    d = previous_release_by[grab["indexer"]]
                    d["releases"] += 1
                    d["completed"] += int(completed)
                    d["failed"] += int(not completed)
                    d["repair"] += int(max_blocks > 0)
                    d["heavy"] += int(max_blocks >= 10)

            previous_release_stats = {}
            for name, v in previous_release_by.items():
                previous_release_stats[name] = {
                    **v,
                    "success": pct(v["completed"], v["releases"]),
                    "repair_pct": pct(v["repair"], v["releases"]),
                    "heavy_pct": pct(v["heavy"], v["releases"]),
                }

            for item in release_stats:
                prev = previous_release_stats.get(item["indexer"])
                item["previous_releases"] = int(prev["releases"]) if prev else 0
                item["previous_completed"] = int(prev["completed"]) if prev else 0
                item["previous_success"] = prev["success"] if prev else None
                item["previous_repair_pct"] = prev["repair_pct"] if prev else None
                item["previous_heavy_pct"] = prev["heavy_pct"] if prev else None
                item["success_delta_pp"] = (
                    round(float(item["success"]) - float(prev["success"]), 2)
                    if prev and prev["success"] is not None and item["success"] is not None
                    else None
                )
                item["repair_delta_pp"] = (
                    round(float(item["repair_pct"]) - float(prev["repair_pct"]), 2)
                    if prev and prev["repair_pct"] is not None and item["repair_pct"] is not None
                    else None
                )
                item["heavy_delta_pp"] = (
                    round(float(item["heavy_pct"]) - float(prev["heavy_pct"]), 2)
                    if prev and prev["heavy_pct"] is not None and item["heavy_pct"] is not None
                    else None
                )

            # ---------------------------------------------------------------
            # Recovery credit: who rescued releases, and whose failures were
            # later rescued elsewhere.
            # ---------------------------------------------------------------
            recovery_credit_map = defaultdict(lambda: {
                "rescued_from_others": 0,
                "rescued_elsewhere": 0,
                "same_indexer_recoveries": 0,
            })
            recovery_paths = defaultdict(int)
            for item in recoveries:
                failed_via = item["failed_via"]
                recovered_via = item["recovered_via"]
                recovery_paths[(failed_via, recovered_via)] += 1
                if failed_via == recovered_via:
                    recovery_credit_map[recovered_via]["same_indexer_recoveries"] += 1
                else:
                    recovery_credit_map[recovered_via]["rescued_from_others"] += 1
                    recovery_credit_map[failed_via]["rescued_elsewhere"] += 1

            recovery_credit = []
            names = {x["name"] for x in indexers}
            names.update(recovery_credit_map.keys())
            for name in sorted(names):
                v = recovery_credit_map[name]
                recovery_credit.append({"indexer": name, **v})

            recovery_path_summary = [
                {"failed_via": k[0], "recovered_via": k[1], "count": n}
                for k, n in sorted(
                    recovery_paths.items(),
                    key=lambda kv: (-kv[1], kv[0][0].lower(), kv[0][1].lower()),
                )
            ]

            # ---------------------------------------------------------------
            # Sample-aware window verdicts. These are intentionally summaries
            # of the selected Activity Window, not permanent indexer ratings.
            # ---------------------------------------------------------------
            quality_by_name = {x["indexer"]: x for x in release_stats}
            credit_by_name = {x["indexer"]: x for x in recovery_credit}

            for api in indexers:
                name = api["name"]
                q = quality_by_name.get(name)
                credit = credit_by_name.get(name, {
                    "rescued_from_others": 0,
                    "rescued_elsewhere": 0,
                    "same_indexer_recoveries": 0,
                })

                releases_n = int(q["releases"]) if q else 0
                success = float(q["success"] or 0) if q else 0.0
                repair_pct = float(q["repair_pct"] or 0) if q else 0.0
                heavy_pct = float(q["heavy_pct"] or 0) if q else 0.0

                if releases_n == 0:
                    verdict = "No attributed releases in this window"
                    tone = "neutral"
                elif releases_n < 10:
                    verdict = f"Limited sample · {releases_n} releases"
                    tone = "neutral"
                else:
                    if success >= 90 and heavy_pct <= 5:
                        verdict = "Excellent success · low heavy-repair rate"
                        tone = "good"
                    elif success >= 80 and repair_pct <= 30:
                        verdict = "Strong success · low repair burden"
                        tone = "good"
                    elif success >= 70:
                        verdict = "Good success · watch repair burden"
                        tone = "good"
                    elif success >= 50:
                        verdict = "Mixed results in this window"
                        tone = "warn"
                    else:
                        verdict = "Low success in this window"
                        tone = "bad"

                rescued = int(credit.get("rescued_from_others", 0) or 0)
                lost = int(credit.get("rescued_elsewhere", 0) or 0)
                same = int(credit.get("same_indexer_recoveries", 0) or 0)

                notes = []
                if rescued:
                    notes.append(f"rescued {rescued} from other indexers")
                if lost:
                    notes.append(f"{lost} later rescued elsewhere")
                if same:
                    notes.append(f"{same} same-index recoveries")

                api["window_verdict"] = verdict
                api["window_verdict_tone"] = tone
                api["window_verdict_note"] = " · ".join(notes)
                api["window_verdict_sample"] = releases_n

            # ---------------------------------------------------------------
            # Failure severity is unique-release based, not attempt based.
            # ---------------------------------------------------------------
            failure_severity = {"zero": 0, "light": 0, "heavy": 0, "total": 0}
            failure_severity_by = defaultdict(lambda: {"zero": 0, "light": 0, "heavy": 0, "total": 0})
            for item in releases:
                if item["outcome"] != "failed":
                    continue
                blocks = int(item["repair_blocks"] or 0)
                bucket = "zero" if blocks == 0 else "light" if blocks < 10 else "heavy"
                failure_severity[bucket] += 1
                failure_severity["total"] += 1
                idx = item["indexer"]
                failure_severity_by[idx][bucket] += 1
                failure_severity_by[idx]["total"] += 1

            failure_severity_by_indexer = [
                {"indexer": name, **vals}
                for name, vals in sorted(failure_severity_by.items())
            ]

            # ---------------------------------------------------------------
            # Time-bucketed indexer metrics for trend charts.
            # ---------------------------------------------------------------
            quality_bucket_hours = trend_bucket_hours
            metric_buckets = defaultdict(lambda: {
                "attempts": 0, "attempt_completed": 0,
                "releases": 0, "release_completed": 0,
                "repair": 0, "heavy": 0,
            })

            def bucket_key(indexer_name: str, dt: datetime):
                return indexer_name, trend_bucket(dt)

            for item in attempts:
                # Trend denominators intentionally mirror the quality cards:
                # user/system cancellations and unresolved activity are visible
                # elsewhere but must not depress an indexer's success rate.
                if item["indexer"] == "(unattributed)" or item.get("status") not in {"completed", "failed"}:
                    continue
                dt = self._local_dt(item["time"])
                if dt is None:
                    continue
                vals = metric_buckets[bucket_key(item["indexer"], dt)]
                vals["attempts"] += 1
                vals["attempt_completed"] += int(item["status"] == "completed")

            for item in releases:
                if item["indexer"] == "(unattributed)" or item.get("outcome") not in {"completed", "recovered", "failed"}:
                    continue
                dt = self._local_dt(item["final_time"])
                if dt is None:
                    continue
                vals = metric_buckets[bucket_key(item["indexer"], dt)]
                vals["releases"] += 1
                vals["release_completed"] += int(item["eventually_completed"])
                vals["repair"] += int(item["repair_blocks"] > 0)
                vals["heavy"] += int(item["repair_blocks"] >= 10)

            indexer_metric_series = []
            for (name, bucket_time), vals in sorted(
                metric_buckets.items(), key=lambda kv: (kv[0][1], kv[0][0].lower())
            ):
                indexer_metric_series.append({
                    "time": bucket_time.isoformat(timespec="seconds"),
                    "indexer": name,
                    "attempts": vals["attempts"],
                    "releases": vals["releases"],
                    "final_success": pct(vals["release_completed"], vals["releases"]),
                    "attempt_success": pct(vals["attempt_completed"], vals["attempts"]),
                    "repair_rate": pct(vals["repair"], vals["releases"]),
                    "heavy_repair_rate": pct(vals["heavy"], vals["releases"]),
                })

            # Backwards-compatible alias used by older JS/templates.
            indexer_quality_series = [
                {
                    "time": x["time"], "indexer": x["indexer"],
                    "releases": x["releases"], "success": x["final_success"],
                }
                for x in indexer_metric_series
            ]

            # Dashboard trend: mutually exclusive final acquisition outcomes.
            # Recovered means a failed attempt was followed by completion, so it
            # is kept separate from clean completions rather than double-counted.
            acquisition_buckets: dict[datetime, dict[str, int]] = defaultdict(
                lambda: {"Completed": 0, "Recovered": 0, "Failed-only": 0}
            )
            for item in releases:
                outcome = str(item.get("outcome") or "")
                label = {"completed": "Completed", "recovered": "Recovered", "failed": "Failed-only"}.get(outcome)
                if not label:
                    continue
                rdt = self._local_dt(item.get("final_time"))
                if rdt is None:
                    continue
                acquisition_buckets[trend_bucket(rdt)][label] += 1

            # Fill empty buckets so selected-window trends do not visually join
            # activity across large silent gaps.
            bucket_cursor = activity_cutoff
            while bucket_cursor <= now:
                acquisition_buckets.setdefault(bucket_cursor, {"Completed": 0, "Recovered": 0, "Failed-only": 0})
                bucket_cursor += trend_step
            acquisition_outcome_series = []
            for bucket_time, vals in sorted(acquisition_buckets.items()):
                for label in ("Completed", "Recovered", "Failed-only"):
                    acquisition_outcome_series.append({
                        "time": bucket_time.isoformat(timespec="seconds"),
                        "series": label,
                        "value": int(vals.get(label, 0)),
                        "sample": int(vals.get(label, 0)),
                    })

            dashboard_indexer_success_series = [
                {
                    "time": x["time"],
                    "series": x["indexer"],
                    "value": x["final_success"],
                    "sample": x["releases"],
                }
                for x in indexer_metric_series
                if x.get("final_success") is not None and int(x.get("releases") or 0) > 0
            ]

            total_attempts = len(attempts)
            completed_attempts = sum(x["status"] == "completed" for x in attempts)
            failed_attempts = sum(x["status"] == "failed" for x in attempts)
            cancelled_attempts = sum(x["status"] == "cancelled" for x in attempts)
            quality_attempts = completed_attempts + failed_attempts
            total_releases = len(releases)
            quality_releases = [x for x in releases if x["outcome"] in {"completed", "recovered", "failed"}]
            quality_release_count = len(quality_releases)
            cancelled_releases = sum(x["outcome"] == "cancelled" for x in releases)
            eventually = sum(x["eventually_completed"] for x in quality_releases)
            failed_only = sum(x["outcome"] == "failed" for x in quality_releases)
            recovered_count = sum(x["recovered"] for x in quality_releases)
            repaired_releases = sum(x["repair_blocks"] > 0 for x in quality_releases)
            heavy_releases = sum(x["repair_blocks"] >= 10 for x in quality_releases)

            previous_attempt_success = pct(
                previous_completed_attempts, len(previous_attempts)
            )
            previous_eventual_success = pct(
                previous_release_completed, previous_release_total
            )
            current_attempt_success = pct(completed_attempts, quality_attempts)
            current_eventual_success = pct(eventually, quality_release_count)

            sab = {
                "attempts": total_attempts,
                "quality_attempts": quality_attempts,
                "completed_attempts": completed_attempts,
                "failed_attempts": failed_attempts,
                "cancelled_attempts": cancelled_attempts,
                "attempt_success": current_attempt_success,
                "previous_attempts": len(previous_attempts),
                "previous_completed_attempts": previous_completed_attempts,
                "previous_attempt_success": previous_attempt_success,
                "attempt_success_delta_pp": (
                    round(float(current_attempt_success) - float(previous_attempt_success), 2)
                    if current_attempt_success is not None and previous_attempt_success is not None
                    else None
                ),
                "unique_releases": total_releases,
                "quality_unique_releases": quality_release_count,
                "cancelled_releases": cancelled_releases,
                "eventually_completed": eventually,
                "failed_only": failed_only,
                "recovered": recovered_count,
                "eventual_success": current_eventual_success,
                "previous_unique_releases": previous_release_total,
                "previous_eventually_completed": previous_release_completed,
                "previous_eventual_success": previous_eventual_success,
                "eventual_success_delta_pp": (
                    round(float(current_eventual_success) - float(previous_eventual_success), 2)
                    if current_eventual_success is not None and previous_eventual_success is not None
                    else None
                ),
                "repaired_releases": repaired_releases,
                "repair_pct": pct(repaired_releases, quality_release_count),
                "heavy_repair_releases": heavy_releases,
                "heavy_pct": pct(heavy_releases, quality_release_count),
            }

            notable_changes = []
            if sab["attempt_success_delta_pp"] is not None:
                notable_changes.append({
                    "label": "Attempt success",
                    "delta_pp": sab["attempt_success_delta_pp"],
                    "current_sample": sab["quality_attempts"],
                    "previous_sample": sab["previous_attempts"],
                    "path": "/downloads",
                })
            if sab["eventual_success_delta_pp"] is not None:
                notable_changes.append({
                    "label": "Eventual success",
                    "delta_pp": sab["eventual_success_delta_pp"],
                    "current_sample": sab["quality_unique_releases"],
                    "previous_sample": sab["previous_unique_releases"],
                    "path": "/releases",
                })
            for q in release_stats:
                if q.get("success_delta_pp") is None:
                    continue
                notable_changes.append({
                    "label": f"{q['indexer']} final success",
                    "delta_pp": q["success_delta_pp"],
                    "current_sample": q["releases"],
                    "previous_sample": q["previous_releases"],
                    "path": f"/releases?indexer={q['indexer']}",
                })
            notable_changes.sort(key=lambda x: abs(float(x["delta_pp"])), reverse=True)
            notable_changes = notable_changes[:3]

            health_summary = []
            for api in sorted(indexers, key=lambda x: x["name"].lower()):
                q = quality_by_name.get(api["name"], {})
                health_summary.append({
                    "label": api["name"],
                    "value": api.get("window_verdict") or "No verdict",
                    "tone": api.get("window_verdict_tone") or "neutral",
                    "detail": f"{q.get('releases', 0)} releases",
                    "path": f"/releases?indexer={api['name']}",
                })
            health_summary.append({
                "label": "Unresolved failures",
                "value": str(sab["failed_only"]),
                "tone": "bad" if sab["failed_only"] else "good",
                "detail": "failed-only releases",
                "path": "/failures",
            })

            recent_failed_only = sorted(
                [x for x in releases if x["outcome"] == "failed"],
                key=lambda x: x["final_time"],
                reverse=True,
            )

            # Causal failure summary: unique failed releases, using the final
            # attempt's reconstructed SAB lifecycle. Unknown means SAB marked
            # the attempt failed but neither retained log nor History API data
            # carried a more specific causal message.
            failure_cause_counts = defaultdict(int)
            for item in recent_failed_only:
                stage = str(item.get("failure_stage") or "Cause unavailable")
                if stage == "Unknown":
                    stage = "Cause unavailable"
                failure_cause_counts[stage] += 1
            failure_causes = [
                {"stage": stage, "count": count}
                for stage, count in sorted(
                    failure_cause_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())
                )
            ]

            # Diagnostics
            diagnostics = [
                {
                    "indexer": r["indexer_name"],
                    "kind": r["kind"],
                    "count": int(r["n"]),
                }
                for r in conn.execute(
                    """
                    SELECT COALESCE(indexer_name,'(unknown)') indexer_name,kind,COUNT(*) n
                    FROM prowlarr_log_errors
                    GROUP BY COALESCE(indexer_name,'(unknown)'),kind
                    ORDER BY n DESC,indexer_name
                    """
                ).fetchall()
            ]

            # Arr orchestration diagnostics inside the selected activity window.
            arr_event_counts = {
                "search_started": 0, "retry_search_intent": 0, "rss_started": 0,
                "sent_to_sab": 0, "grab_failed": 0,
                "import_succeeded": 0, "import_failed": 0, "import_cancelled": 0,
                "download_cancelled": 0, "history_removed": 0, "sab_unreachable": 0,
            }
            if self._table_exists(conn, "arr_events"):
                arr_rows = conn.execute(
                    """SELECT event_time,app,event_type,release_name,indexer,reason,message
                       FROM arr_events ORDER BY event_time DESC"""
                ).fetchall()
                for r in arr_rows:
                    dt = self._local_dt(r["event_time"])
                    if dt is None or dt < activity_cutoff:
                        continue
                    et = str(r["event_type"] or "").lower()
                    if et in arr_event_counts:
                        arr_event_counts[et] += 1

            # Last collector run. v7.8 writes one summary row for the whole
            # rotating SAB log family; older databases fall back to log_runs.
            last_sab_family = None
            if self._table_exists(conn, "sab_log_family_runs"):
                last_sab_family = conn.execute(
                    "SELECT * FROM sab_log_family_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            last_sab = conn.execute("SELECT * FROM log_runs ORDER BY id DESC LIMIT 1").fetchone()
            last_prow = conn.execute("SELECT * FROM prowlarr_log_runs ORDER BY id DESC LIMIT 1").fetchone()
            last_prow_family = None
            if self._table_exists(conn, "prowlarr_log_family_runs"):
                last_prow_family = conn.execute(
                    "SELECT * FROM prowlarr_log_family_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            last_arr = {}
            if self._table_exists(conn, "arr_log_family_runs"):
                for app_name in ("sonarr", "radarr"):
                    row = conn.execute(
                        "SELECT * FROM arr_log_family_runs WHERE app=? ORDER BY id DESC LIMIT 1",
                        (app_name,),
                    ).fetchone()
                    if row:
                        last_arr[app_name] = row
            last_collector_meta = None
            if self._table_exists(conn, "collector_runs"):
                last_collector_meta = conn.execute(
                    "SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            cancel_reconcile = {
                "applied_at": None, "jobs": 0, "events": 0, "lines_scanned": 0,
                "remove_candidates": 0, "purge_true_candidates": 0, "direct_unpack_abort": 0,
                "accepted": 0, "inserted": 0, "reclassified": 0, "already_recorded": 0,
                "rejected_ending": 0, "rejected_postproc": 0, "rejected_no_purge": 0,
            }
            if self._table_exists(conn, "collector_migrations"):
                cm = conn.execute(
                    "SELECT applied_at,details FROM collector_migrations WHERE name='sab_cancel_reconcile_v785'"
                ).fetchone()
                if cm:
                    cancel_reconcile["applied_at"] = cm["applied_at"]
                    try:
                        details = json.loads(cm["details"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        details = {}
                    for key in cancel_reconcile:
                        if key != "applied_at":
                            cancel_reconcile[key] = int(details.get(key, 0) or 0)

            provider_delta = []
            for p in conn.execute(
                "SELECT DISTINCT provider FROM provider_snapshots ORDER BY provider"
            ).fetchall():
                rr = conn.execute(
                    """
                    SELECT * FROM provider_snapshots
                    WHERE provider=?
                    ORDER BY id DESC
                    LIMIT 2
                    """,
                    (p["provider"],),
                ).fetchall()
                if not rr:
                    continue
                cur = rr[0]
                prev = rr[1] if len(rr) > 1 else None
                provider_delta.append({
                    "provider": p["provider"],
                    "day_bytes_delta": self._counter_delta(cur, prev, "day_bytes"),
                    "day_delta_human": human_bytes(self._counter_delta(cur, prev, "day_bytes")),
                    "tried_delta": self._counter_delta(cur, prev, "articles_tried"),
                    "secondary_delta": self._counter_delta(cur, prev, "articles_success"),
                })

            indexer_delta = []
            for r in conn.execute(
                "SELECT DISTINCT indexer_id FROM prowlarr_stats_snapshots ORDER BY indexer_id"
            ).fetchall():
                rr = conn.execute(
                    """
                    SELECT * FROM prowlarr_stats_snapshots
                    WHERE indexer_id=?
                    ORDER BY id DESC
                    LIMIT 2
                    """,
                    (int(r["indexer_id"]),),
                ).fetchall()
                if not rr:
                    continue
                cur = rr[0]
                prev = rr[1] if len(rr) > 1 else None
                indexer_delta.append({
                    "indexer": cur["indexer_name"],
                    "queries_delta": self._counter_delta(cur, prev, "queries"),
                    "grabs_delta": self._counter_delta(cur, prev, "grabs"),
                })

            sab_run = last_sab_family or last_sab
            last_collection = {
                "collected_at": sab_run["collected_at"] if sab_run else None,
                "collector_version": self._row_value(last_collector_meta, "version", None),
                "collector_script": self._row_value(last_collector_meta, "script_path", None),
                "collector_db": self._row_value(last_collector_meta, "db_path", None),
                "cancel_reconcile_applied_at": cancel_reconcile["applied_at"],
                "cancel_reconcile_jobs": cancel_reconcile["jobs"],
                "cancel_reconcile_events": cancel_reconcile["events"],
                "cancel_reconcile_lines": cancel_reconcile["lines_scanned"],
                "cancel_remove_candidates": cancel_reconcile["remove_candidates"],
                "cancel_purge_true_candidates": cancel_reconcile["purge_true_candidates"],
                "cancel_direct_unpack_abort": cancel_reconcile["direct_unpack_abort"],
                "cancel_accepted": cancel_reconcile["accepted"],
                "cancel_inserted": cancel_reconcile["inserted"],
                "cancel_reclassified": cancel_reconcile["reclassified"],
                "cancel_already_recorded": cancel_reconcile["already_recorded"],
                "cancel_rejected_ending": cancel_reconcile["rejected_ending"],
                "cancel_rejected_postproc": cancel_reconcile["rejected_postproc"],
                "cancel_rejected_no_purge": cancel_reconcile["rejected_no_purge"],
                "sab_completed": int(sab_run["completed"] or 0) if sab_run else 0,
                "sab_failed": int(sab_run["failed"] or 0) if sab_run else 0,
                "sab_live_completed": int(self._row_value(last_sab_family, "live_completed", sab_run["completed"] if sab_run else 0) or 0),
                "sab_backfill_completed": int(self._row_value(last_sab_family, "backfill_completed", 0) or 0),
                "sab_live_failed": int(self._row_value(last_sab_family, "live_failed", sab_run["failed"] if sab_run else 0) or 0),
                "sab_backfill_failed": int(self._row_value(last_sab_family, "backfill_failed", 0) or 0),
                "sab_cancelled": int(self._row_value(last_sab_family, "cancelled", 0) or 0),
                "sab_live_cancelled": int(self._row_value(last_sab_family, "live_cancelled", 0) or 0),
                "sab_backfill_cancelled": int(self._row_value(last_sab_family, "backfill_cancelled", 0) or 0),
                "sab_live_lifecycle_events": int(self._row_value(last_sab_family, "live_lifecycle_events", self._row_value(last_sab_family, "lifecycle_events", 0)) or 0),
                "sab_backfill_lifecycle_events": int(self._row_value(last_sab_family, "backfill_lifecycle_events", 0) or 0),
                "sab_lines": int(sab_run["lines_read"] or 0) if sab_run else 0,
                "unavailable": int(sab_run["unavailable_events"] or 0) if sab_run else 0,
                "repair_events": int(sab_run["repair_events"] or 0) if sab_run else 0,
                "sab_segments_found": int(last_sab_family["segments_found"] or 0) if last_sab_family else 1 if last_sab else 0,
                "sab_segments_read": int(last_sab_family["segments_read"] or 0) if last_sab_family else 1 if last_sab else 0,
                "sab_new_segments": int(last_sab_family["new_segments"] or 0) if last_sab_family else 0,
                "sab_lifecycle_events": int(last_sab_family["lifecycle_events"] or 0) if last_sab_family else 0,
                "sab_continuity_status": str(self._row_value(last_sab_family, "continuity_status", "unknown") or "unknown") if last_sab_family else "unknown",
                "sab_continuity_message": str(self._row_value(last_sab_family, "continuity_message", "") or "") if last_sab_family else "",
                "sab_continuity_overlap": int(self._row_value(last_sab_family, "continuity_overlap", 0) or 0) if last_sab_family else 0,
                "sab_oldest_first_time": self._row_value(last_sab_family, "oldest_first_time", None) if last_sab_family else None,
                "sab_newest_first_time": self._row_value(last_sab_family, "newest_first_time", None) if last_sab_family else None,
                "prowlarr_searches": int(last_prow["searches"] or 0) if last_prow else 0,
                "prowlarr_live_searches": int(self._row_value(last_prow_family, "live_searches", last_prow["searches"] if last_prow else 0) or 0),
                "prowlarr_backfill_searches": int(self._row_value(last_prow_family, "backfill_searches", 0) or 0),
                "prowlarr_live_diagnostics": int(self._row_value(last_prow_family, "live_diagnostics", 0) or 0),
                "prowlarr_backfill_diagnostics": int(self._row_value(last_prow_family, "backfill_diagnostics", 0) or 0),
                "prowlarr_invalid": int(last_prow["invalid_releases"] or 0) if last_prow else 0,
                "prowlarr_connection_failures": int(last_prow["connection_failures"] or 0) if last_prow else 0,
                "prowlarr_segments_found": int(last_prow_family["segments_found"] or 0) if last_prow_family else 1 if last_prow else 0,
                "prowlarr_segments_read": int(last_prow_family["segments_read"] or 0) if last_prow_family else 1 if last_prow else 0,
                "prowlarr_new_segments": int(last_prow_family["new_segments"] or 0) if last_prow_family else 0,
                "sonarr_segments_found": int(last_arr.get("sonarr")["segments_found"] or 0) if last_arr.get("sonarr") else 0,
                "sonarr_segments_read": int(last_arr.get("sonarr")["segments_read"] or 0) if last_arr.get("sonarr") else 0,
                "sonarr_new_segments": int(last_arr.get("sonarr")["new_segments"] or 0) if last_arr.get("sonarr") else 0,
                "sonarr_lines": int(last_arr.get("sonarr")["lines_read"] or 0) if last_arr.get("sonarr") else 0,
                "sonarr_events": int(last_arr.get("sonarr")["relevant_events"] or 0) if last_arr.get("sonarr") else 0,
                "sonarr_live_events": int(self._row_value(last_arr.get("sonarr"), "live_events", self._row_value(last_arr.get("sonarr"), "relevant_events", 0)) or 0),
                "sonarr_backfill_events": int(self._row_value(last_arr.get("sonarr"), "backfill_events", 0) or 0),
                "radarr_segments_found": int(last_arr.get("radarr")["segments_found"] or 0) if last_arr.get("radarr") else 0,
                "radarr_segments_read": int(last_arr.get("radarr")["segments_read"] or 0) if last_arr.get("radarr") else 0,
                "radarr_new_segments": int(last_arr.get("radarr")["new_segments"] or 0) if last_arr.get("radarr") else 0,
                "radarr_lines": int(last_arr.get("radarr")["lines_read"] or 0) if last_arr.get("radarr") else 0,
                "radarr_events": int(last_arr.get("radarr")["relevant_events"] or 0) if last_arr.get("radarr") else 0,
                "radarr_live_events": int(self._row_value(last_arr.get("radarr"), "live_events", self._row_value(last_arr.get("radarr"), "relevant_events", 0)) or 0),
                "radarr_backfill_events": int(self._row_value(last_arr.get("radarr"), "backfill_events", 0) or 0),
                "provider_delta": provider_delta,
                "indexer_delta": indexer_delta,
            }

            search_coverage = self._search_coverage(conn, activity_cutoff)

            # Window-aware raw wanted-quality coverage trend. Each target-known
            # logical search contributes one binary opportunity per queried
            # Usenet/NZB indexer. Indexer names come from retained Prowlarr
            # metadata/search evidence; nothing here assumes a fixed provider set.
            coverage_buckets: dict[tuple[str, datetime], dict[str, int]] = defaultdict(lambda: {"opportunities": 0, "hits": 0})
            for session in search_coverage.get("sessions", []):
                if not session.get("target_known"):
                    continue
                sdt = self._local_dt(session.get("time"))
                if sdt is None:
                    continue
                btime = trend_bucket(sdt)
                for idx in session.get("indexers", []):
                    hit = idx.get("target_hit")
                    if hit is None:
                        continue
                    name = str(idx.get("indexer") or "").strip()
                    if not name:
                        continue
                    bucket = coverage_buckets[(name, btime)]
                    bucket["opportunities"] += 1
                    bucket["hits"] += int(bool(hit))
            dashboard_search_coverage_series = [
                {
                    "time": btime.isoformat(timespec="seconds"),
                    "series": name,
                    "value": pct(vals["hits"], vals["opportunities"]),
                    "sample": vals["opportunities"],
                }
                for (name, btime), vals in sorted(coverage_buckets.items(), key=lambda kv: (kv[0][1], kv[0][0].lower()))
                if vals["opportunities"]
            ]

            # Evidence health replaces the old all-Prowlarr-error table. It
            # summarizes whether the retained sources are strong enough to trust
            # the dashboard without elevating unrelated torrent-indexer noise.
            coverage_sessions = search_coverage.get("sessions", [])
            exact_targets = sum(1 for x in coverage_sessions if x.get("target_known") and x.get("target_source") == "arr_api_snapshot")
            historical_high = sum(1 for x in coverage_sessions if x.get("target_known") and x.get("target_source") != "arr_api_snapshot" and str(x.get("target_confidence") or "").lower() == "high")
            historical_medium = sum(1 for x in coverage_sessions if x.get("target_known") and x.get("target_source") != "arr_api_snapshot" and str(x.get("target_confidence") or "").lower() == "medium")
            target_known = int(search_coverage.get("target_known_sessions", 0) or 0)
            target_total = len(coverage_sessions)
            unresolved_targets = int(search_coverage.get("unresolved_sessions", 0) or 0)
            attributed_total = int(release_conf.get("exact", 0) or 0) + int(release_conf.get("episode", 0) or 0) + int(release_conf.get("unattributed", 0) or 0)
            attributed_good = int(release_conf.get("exact", 0) or 0) + int(release_conf.get("episode", 0) or 0)

            collector_dt = self._local_dt(last_collection.get("collected_at"))
            collector_age_minutes = None
            if collector_dt is not None:
                collector_age_minutes = max(0, int((now - collector_dt).total_seconds() // 60))
            if collector_age_minutes is None:
                collector_age_text = "unknown"
                collector_age_tone = "warn"
            elif collector_age_minutes < 60:
                collector_age_text = f"{collector_age_minutes}m ago"
                collector_age_tone = "good"
            elif collector_age_minutes < 360:
                collector_age_text = f"{collector_age_minutes // 60}h {collector_age_minutes % 60}m ago"
                collector_age_tone = "warn"
            else:
                collector_age_text = f"{collector_age_minutes // 60}h ago"
                collector_age_tone = "bad"

            sab_continuity = str(last_collection.get("sab_continuity_status") or "unknown")
            evidence_health = [
                {
                    "label": "SAB retention continuity",
                    "value": sab_continuity.replace("_", " "),
                    "detail": last_collection.get("sab_continuity_message") or f"{last_collection.get('sab_continuity_overlap', 0)} retained segment overlap",
                    "tone": "good" if sab_continuity == "ok" else "warn" if sab_continuity in {"possible_gap", "unverified", "unknown"} else "neutral",
                },
                {
                    "label": "Search target evidence",
                    "value": f"{target_known:,} / {target_total:,} known",
                    "detail": f"{exact_targets:,} exact API · {historical_high:,} historical high · {historical_medium:,} historical medium · {unresolved_targets:,} unresolved",
                    "tone": "good" if target_total and unresolved_targets == 0 else "warn" if target_known else "neutral",
                },
                {
                    "label": "Release attribution",
                    "value": f"{pct(attributed_good, attributed_total) or 0:.1f}% attributed" if attributed_total else "no sample",
                    "detail": f"{int(release_conf.get('exact', 0) or 0):,} exact · {int(release_conf.get('episode', 0) or 0):,} episode fallback · {int(release_conf.get('unattributed', 0) or 0):,} unattributed",
                    "tone": "good" if attributed_total and int(release_conf.get("unattributed", 0) or 0) == 0 else "warn" if attributed_total else "neutral",
                },
                {
                    "label": "Collector freshness",
                    "value": collector_age_text,
                    "detail": str(last_collection.get("collector_version") or "collector version unknown"),
                    "tone": collector_age_tone,
                },
                {
                    "label": "Retained log families",
                    "value": f"SAB {last_collection.get('sab_segments_found', 0)} · Prowlarr {last_collection.get('prowlarr_segments_found', 0)}",
                    "detail": f"Sonarr {last_collection.get('sonarr_segments_found', 0)} · Radarr {last_collection.get('radarr_segments_found', 0)} segments in latest observation",
                    "tone": "neutral",
                },
            ]

            coverage_by_name = {str(x.get("indexer") or "").lower(): x for x in search_coverage.get("indexers", [])}
            for item in indexers:
                cov = coverage_by_name.get(str(item.get("name") or "").lower())
                if cov:
                    item["search_coverage"] = cov
                else:
                    item["search_coverage"] = {
                        "indexer": item.get("name"), "searches": 0, "opportunities": 0,
                        "target_opportunities": 0, "target_hits": 0, "target_misses": 0,
                        "availability_score": None, "target_coverage": None,
                        "matching_results": 0, "raw_candidates": 0, "selected": 0,
                        "target_tiers": {}, "raw_tiers": {},
                        "wanted_opportunities": 0, "wanted_hits": 0, "wanted_misses": 0,
                        "wanted_coverage": None, "discovery_score": None,
                        "result_count_total": 0, "tiers": {},
                    }

            counts = {}
            for name, table in (
                ("provider_snapshots", "provider_snapshots"),
                ("sab_attempts", "jobs"),
                ("sab_history", "sab_history"),
                ("sab_log_segments", "sab_log_segments"),
                ("sab_log_events", "sab_log_events"),
                ("sab_attempt_diagnostics", "sab_attempt_diagnostics"),
                ("prowlarr_history", "prowlarr_history"),
                ("prowlarr_searches", "prowlarr_log_searches"),
                ("prowlarr_log_segments", "prowlarr_log_segments"),
                ("arr_events", "arr_events"),
                ("arr_search_candidates", "arr_search_candidates"),
                ("arr_log_segments", "arr_log_segments"),
                ("diagnostics", "prowlarr_log_errors"),
            ):
                try:
                    counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[name] = 0

            source_errors = {}
            if last_collector_meta is not None and "source_errors_json" in last_collector_meta.keys():
                try:
                    source_errors = json.loads(last_collector_meta["source_errors_json"] or "{}")
                except (ValueError, TypeError):
                    pass
            return {
                "generated_at": local_now().isoformat(timespec="seconds"),
                "source_errors": source_errors,
                "hours": hours,
                "providers": providers,
                "provider_series": provider_series,
                "provider_availability_series": provider_availability_series,
                "provider_contribution": provider_contribution,
                "provider_contribution_series": provider_contribution_series,
                "indexers": indexers,
                "search_coverage": search_coverage,
                "dashboard_search_coverage_series": dashboard_search_coverage_series,
                "sab": sab,
                "attempts": list(reversed(attempts)),
                "attempt_stats": attempt_stats,
                "releases": sorted(releases, key=lambda x: x["final_time"], reverse=True),
                "release_stats": release_stats,
                "recoveries": list(reversed(recoveries)),
                "recovery_credit": recovery_credit,
                "recovery_path_summary": recovery_path_summary,
                "failure_severity": failure_severity,
                "failure_severity_by_indexer": failure_severity_by_indexer,
                "failure_causes": failure_causes,
                "indexer_quality_series": indexer_quality_series,
                "indexer_metric_series": indexer_metric_series,
                "dashboard_indexer_success_series": dashboard_indexer_success_series,
                "acquisition_outcome_series": acquisition_outcome_series,
                "quality_bucket_hours": quality_bucket_hours,
                "health_summary": health_summary,
                "evidence_health": evidence_health,
                "notable_changes": notable_changes,
                "recent_failed_only": recent_failed_only,
                "attempt_confidence": attempt_conf,
                "release_confidence": release_conf,
                "diagnostics": diagnostics,
                "arr_event_counts": arr_event_counts,
                "last_collection": last_collection,
                "counts": counts,
            }


    def _coverage_from_sessions(self, sessions: list[dict[str, Any]], tiers: list[str] | None = None) -> dict[str, Any]:
        """Re-aggregate Raw Search Results after applying a count-based sample.

        ``_search_coverage`` owns the evidence reconstruction.  This helper only
        changes the denominator by taking an already reconstructed set of logical
        searches and rebuilding the per-indexer counters from those sessions.
        """
        tiers = list(tiers or ["2160p", "1080p", "720p", "SD"])
        acc: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "searches": 0,
            "target_opportunities": 0,
            "target_hits": 0,
            "matching_results": 0,
            "raw_candidates": 0,
            "selected": 0,
            "target_tier_results": defaultdict(int),
            "raw_tier_results": defaultdict(int),
        })
        for session in sessions:
            target_known = bool(session.get("target_known"))
            for item in session.get("indexers", []):
                name = str(item.get("indexer") or "").strip()
                if not name:
                    continue
                bucket = acc[name]
                bucket["searches"] += 1
                bucket["raw_candidates"] += int(item.get("raw_count") or 0)
                bucket["selected"] += int(item.get("selected_count") or 0)
                for tier, value in (item.get("raw_tier_counts") or {}).items():
                    bucket["raw_tier_results"][tier] += int(value or 0)
                if target_known:
                    bucket["target_opportunities"] += 1
                    target_count = int(item.get("target_count") or 0)
                    bucket["matching_results"] += target_count
                    bucket["target_hits"] += int(target_count > 0)
                    for tier, value in (item.get("matching_tier_counts") or {}).items():
                        bucket["target_tier_results"][tier] += int(value or 0)

        indexers = []
        for name, vals in sorted(acc.items(), key=lambda kv: kv[0].lower()):
            opps = int(vals["target_opportunities"])
            hits = int(vals["target_hits"])
            coverage = pct(hits, opps) if opps else None
            indexers.append({
                "indexer": name,
                "searches": int(vals["searches"]),
                "opportunities": int(vals["searches"]),
                "target_opportunities": opps,
                "target_hits": hits,
                "target_misses": opps - hits,
                "availability_score": coverage,
                "target_coverage": coverage,
                "matching_results": int(vals["matching_results"]),
                "raw_candidates": int(vals["raw_candidates"]),
                "selected": int(vals["selected"]),
                "target_tiers": {tier: {"results": int(vals["target_tier_results"][tier])} for tier in tiers},
                "raw_tiers": {tier: {"results": int(vals["raw_tier_results"][tier])} for tier in tiers},
                "wanted_opportunities": opps,
                "wanted_hits": hits,
                "wanted_misses": opps - hits,
                "wanted_coverage": coverage,
                "discovery_score": coverage,
                "result_count_total": int(vals["raw_candidates"]),
                "tiers": {tier: {"hits": int(vals["target_tier_results"][tier])} for tier in tiers},
            })

        known = sum(bool(x.get("target_known")) for x in sessions)
        misses = [x for x in sessions if x.get("global_target_miss")]
        return {
            "sessions": sessions,
            "indexers": indexers,
            "global_target_misses": misses,
            "global_wanted_gaps": misses,
            "quality_tiers": tiers,
            "target_known_sessions": known,
            "unresolved_sessions": len(sessions) - known,
        }

    def _recent_search_coverage(self, conn: sqlite3.Connection, limit: int) -> tuple[dict[str, Any], int]:
        """Return the newest N analyzable logical Arr searches, independent of time.

        We progressively widen the retained-search probe only when searches lack
        usable Usenet/NZB fan-out evidence.  The expensive reconstruction therefore
        remains proportional to the requested sample in the common case.
        """
        total = int(conn.execute(
            "SELECT COUNT(*) FROM arr_events WHERE event_type='search_started'"
        ).fetchone()[0]) if self._table_exists(conn, "arr_events") else 0
        if total <= 0:
            return self._coverage_from_sessions([]), 0

        probe = min(total, max(limit * 2, limit + 50))
        coverage: dict[str, Any] = self._coverage_from_sessions([])
        while True:
            row = conn.execute(
                """SELECT event_time FROM arr_events
                   WHERE event_type='search_started'
                   ORDER BY event_time DESC LIMIT 1 OFFSET ?""",
                (max(0, probe - 1),),
            ).fetchone()
            if row is None:
                break
            cutoff = self._local_dt(row["event_time"])
            if cutoff is None:
                cutoff = datetime(1970, 1, 1).astimezone()
            coverage = self._search_coverage(conn, cutoff - timedelta(seconds=1))
            sessions = list(coverage.get("sessions", []))[:limit]
            if len(sessions) >= limit or probe >= total:
                coverage = self._coverage_from_sessions(sessions, coverage.get("quality_tiers"))
                break
            probe = min(total, max(probe + 1, probe * 2))
        return coverage, total

    @staticmethod
    def _fallback_media_from_release(title: str) -> dict[str, Any]:
        """Return a human media label when Arr identity is unavailable.

        This is presentation fallback only.  It never changes attribution or
        acquisition grouping.  Episode-shaped releases are reliable enough to show
        as Series · SxxEyy; movie releases use the title portion before a retained
        release year when one exists.
        """
        text = str(title or "").strip()
        ep_match = EPISODE_RE.search(text)
        if ep_match:
            series = text[:ep_match.start()]
            series = re.sub(r"[._-]+", " ", series)
            series = re.sub(r"[()\[\]{}]+", " ", series)
            series = re.sub(r"\b(?:19|20)\d{2}\b", " ", series)
            series = " ".join(series.split()).strip()
            season = int(ep_match.group(1))
            episode = int(ep_match.group(2))
            label = f"{series or 'TV episode'} · S{season:02d}E{episode:02d}"
            return {
                "media_title": series or "TV episode",
                "media_season": season,
                "media_episode": episode,
                "media_label": label,
                "media_app": "sonarr",
                "media_source": "release fallback",
                "media_key": f"sonarr:{normalize_match(series)}:{season}:{episode}",
                "media_is_season_pack": False,
            }

        pack = season_identity(text)
        if pack is not None:
            series, season = pack
            pretty = re.sub(r"[._-]+", " ", text[:SEASON_RE.search(text).start()])
            pretty = re.sub(r"[()\[\]{}]+", " ", pretty)
            pretty = re.sub(r"\b(?:19|20)\d{2}\b", " ", pretty)
            pretty = " ".join(pretty.split()).strip() or series.title()
            return {
                "media_title": pretty,
                "media_season": season,
                "media_episode": None,
                "media_label": f"{pretty} · Season {season}",
                "media_app": "sonarr",
                "media_source": "release fallback",
                "media_key": f"sonarr-season-pack:{normalize_match(pretty)}:{season}",
                "media_is_season_pack": True,
            }

        year_match = re.search(r"(?:^|[ ._\-\[(])((?:19|20)\d{2})(?=$|[ ._\-\])])", text)
        stem = text[:year_match.start(1)] if year_match else text
        stem = re.sub(r"[._-]+", " ", stem)
        stem = re.sub(r"[()\[\]{}]+", " ", stem)
        stem = " ".join(stem.split()).strip(" .-_–—")
        # Without a year, a release string may be mostly codec/source tokens.  In
        # that case preserve the exact release rather than inventing a movie title.
        if not year_match or len(stem) < 3:
            stem = text or "Unknown media"
        return {
            "media_title": stem,
            "media_season": None,
            "media_episode": None,
            "media_label": stem,
            "media_app": "radarr" if year_match else "unknown",
            "media_source": "release fallback",
            "media_key": f"fallback:{normalize_match(stem)}",
            "media_is_season_pack": False,
        }

    def _attach_arr_media_to_attempts(self, conn: sqlite3.Connection, attempts: list[dict[str, Any]]) -> None:
        """Attach the retained Sonarr/Radarr media identity to SAB attempts.

        Downloads are displayed media-first, so prefer exact Arr hand-off evidence.
        Matching is deliberately conservative and one-to-one: exact release title
        first, then same episode/movie identity plus indexer when available.  Missing
        evidence falls back to a display-only label parsed from the release title.
        """
        if not attempts:
            return

        for item in attempts:
            item.update(self._fallback_media_from_release(str(item.get("title") or "")))

        if not self._table_exists(conn, "arr_events"):
            return

        dts = [self._local_dt(x.get("time")) for x in attempts]
        dts = [x for x in dts if x is not None]
        if not dts:
            return
        start = (min(dts) - timedelta(hours=48)).isoformat(timespec="seconds")
        end = (max(dts) + timedelta(minutes=5)).isoformat(timespec="seconds")
        rows = conn.execute(
            """SELECT event_id,event_time,app,event_type,media_title,season,episode,
                      release_name,indexer
               FROM arr_events
               WHERE event_type='sent_to_sab' AND event_time >= ? AND event_time <= ?
               ORDER BY event_time""",
            (start, end),
        ).fetchall()
        if not rows:
            return

        handoffs = []
        for row in rows:
            dt = self._local_dt(row["event_time"])
            if dt is None:
                continue
            handoffs.append({
                "event_id": str(row["event_id"]),
                "dt": dt,
                "app": str(row["app"] or "arr").lower(),
                "media_title": str(row["media_title"] or "").strip(),
                "season": row["season"],
                "episode": row["episode"],
                "release_name": str(row["release_name"] or "").strip(),
                "indexer": str(row["indexer"] or "").strip(),
            })

        exact_by_release: dict[str, list[dict[str, Any]]] = defaultdict(list)
        episode_by_release: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        radarr_handoffs: list[dict[str, Any]] = []
        for h in handoffs:
            if h["release_name"]:
                exact_by_release[normalize_match(h["release_name"])].append(h)
                ep = episode_identity(h["release_name"])
                if ep is not None:
                    episode_by_release[ep].append(h)
            if h["app"] == "radarr":
                radarr_handoffs.append(h)

        used: set[str] = set()
        # Oldest-first ensures multiple retries of the same release consume hand-offs
        # in chronological order rather than all attaching to the newest event.
        for item in sorted(attempts, key=lambda x: str(x.get("time") or "")):
            idt = self._local_dt(item.get("time"))
            if idt is None:
                continue
            title = str(item.get("title") or "")
            item_indexer = str(item.get("indexer") or "").strip().lower()

            def viable(h):
                if h["event_id"] in used:
                    return False
                delta = (idt - h["dt"]).total_seconds()
                return -120 <= delta <= 48 * 3600

            # An exact release-name match is strong hand-off evidence, but the
            # retained Arr media identity can still be wrong when an older/busy
            # orchestration session was correlated to the hand-off incorrectly.
            # Before allowing that hand-off to override the release-derived display
            # identity, require its Sonarr/Radarr media fields to agree with the SAB
            # release title itself.  Otherwise leave this attempt on the conservative
            # release-title fallback (or look for another compatible hand-off).
            exact = [
                h for h in exact_by_release.get(normalize_match(title), [])
                if viable(h)
                and h["media_title"]
                and arr_media_matches_release(h["app"], h["media_title"], h["season"], h["episode"], title)
            ]
            chosen = min(exact, key=lambda h: abs((idt - h["dt"]).total_seconds())) if exact else None
            match_kind = "exact Arr hand-off"

            if chosen is None:
                semantic = []
                ep_key = episode_identity(title)
                semantic_pool = episode_by_release.get(ep_key, []) if ep_key is not None else radarr_handoffs
                for h in semantic_pool:
                    if not viable(h) or not h["media_title"]:
                        continue
                    if not arr_media_matches_release(h["app"], h["media_title"], h["season"], h["episode"], title):
                        continue
                    # If both sides know the indexer, require it.  This prevents a
                    # busy batch of episode downloads from borrowing another handoff.
                    hidx = h["indexer"].lower()
                    if item_indexer and item_indexer != "(unattributed)" and hidx and hidx != item_indexer:
                        continue
                    semantic.append(h)
                if semantic:
                    chosen = min(semantic, key=lambda h: abs((idt - h["dt"]).total_seconds()))
                    match_kind = "media Arr hand-off"

            if chosen is None:
                continue
            used.add(chosen["event_id"])
            season = chosen["season"]
            episode = chosen["episode"]
            label = chosen["media_title"] or item.get("media_label") or title
            if season is not None:
                label += f" · S{int(season):02d}"
                if episode is not None:
                    label += f"E{int(episode):02d}"
            item.update({
                "media_title": chosen["media_title"] or label,
                "media_season": season,
                "media_episode": episode,
                "media_label": label,
                "media_app": chosen["app"],
                "media_source": match_kind,
                "media_key": f"{chosen['app']}:{normalize_match(chosen['media_title'])}:{season if season is not None else ''}:{episode if episode is not None else ''}",
            })

    def _attach_sonarr_season_batches(self, conn: sqlite3.Connection, attempts: list[dict[str, Any]]) -> None:
        """Attach an observed Sonarr season-search session to attempts it actually handed off.

        This is deliberately evidence-based: only ``search_started`` rows whose Sonarr
        scope is a whole season (season set, episode null) can create a season batch.
        Attempts are joined through an exact retained ``sent_to_sab`` release title and
        hand-off time that falls inside that search session. Filename similarity alone
        never creates a season group.
        """
        if not attempts or not self._table_exists(conn, "arr_events"):
            return
        dts = [self._local_dt(x.get("time")) for x in attempts]
        dts = [x for x in dts if x is not None]
        if not dts:
            return
        start = (min(dts) - timedelta(hours=48)).isoformat(timespec="seconds")
        end = (max(dts) + timedelta(minutes=5)).isoformat(timespec="seconds")
        rows = conn.execute(
            """SELECT event_id,event_time,app,component,event_type,search_mode,origin_confidence,
                      media_title,season,episode,release_name,indexer,details_json
               FROM arr_events
               WHERE app='sonarr' AND event_time >= ? AND event_time <= ?
                 AND event_type IN ('search_started','search_completed','sent_to_sab')
               ORDER BY event_time""",
            (start, end),
        ).fetchall()
        if not rows:
            return

        parsed = []
        for r in rows:
            dt = self._local_dt(r["event_time"])
            if dt is None:
                continue
            try:
                details = json.loads(r["details_json"] or "{}")
            except Exception:
                details = {}
            parsed.append({
                "event_id": str(r["event_id"]), "dt": dt, "component": str(r["component"] or ""),
                "event_type": str(r["event_type"] or ""), "search_mode": str(r["search_mode"] or "unknown"),
                "origin_confidence": str(r["origin_confidence"] or "none"),
                "media_title": str(r["media_title"] or "").strip(), "season": r["season"], "episode": r["episode"],
                "release_name": str(r["release_name"] or "").strip(), "indexer": str(r["indexer"] or "").strip(),
                "details": details,
            })

        all_starts = [x for x in parsed if x["event_type"] == "search_started" and x["media_title"]]
        starts = [x for x in all_starts if x["season"] is not None and x["episode"] is None]
        completions = [x for x in parsed if x["event_type"] == "search_completed" and x["season"] is not None and x["episode"] is None]
        sends = [x for x in parsed if x["event_type"] == "sent_to_sab" and x["release_name"]]
        if not starts or not sends:
            return

        sessions = []
        for st in starts:
            same_completions = [
                c for c in completions
                if c["dt"] >= st["dt"] and c["dt"] - st["dt"] <= timedelta(hours=2)
                and normalize_match(c["media_title"]) == normalize_match(st["media_title"])
                and c["season"] == st["season"]
            ]
            done = min(same_completions, key=lambda x: x["dt"]) if same_completions else None
            next_same = [
                n for n in starts if n["dt"] > st["dt"]
                and normalize_match(n["media_title"]) == normalize_match(st["media_title"])
                and n["season"] == st["season"]
            ]
            hard_end = done["dt"] if done else st["dt"] + timedelta(hours=2)
            if next_same:
                hard_end = min(hard_end, min(x["dt"] for x in next_same))
            reports_downloaded = None
            if done:
                try:
                    reports_downloaded = int(done["details"].get("reports_downloaded"))
                except (TypeError, ValueError):
                    reports_downloaded = None
            sessions.append({
                "id": st["event_id"], "start": st["dt"], "end": hard_end,
                "title": st["media_title"], "season": int(st["season"]),
                "mode": st["search_mode"], "confidence": st["origin_confidence"],
                "reports_downloaded": reports_downloaded,
                "completed_time": done["dt"].isoformat(timespec="seconds") if done else None,
            })

        sends_by_release: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for h in sends:
            sends_by_release[normalize_match(h["release_name"])].append(h)

        used_handoffs: set[str] = set()
        for item in sorted(attempts, key=lambda x: str(x.get("time") or "")):
            idt = self._local_dt(item.get("time"))
            title = str(item.get("title") or "")
            if idt is None or not title:
                continue
            candidates = []
            for h in sends_by_release.get(normalize_match(title), []):
                if h["event_id"] in used_handoffs:
                    continue
                delta = (idt - h["dt"]).total_seconds()
                if -120 <= delta <= 48 * 3600:
                    candidates.append(h)
            if not candidates:
                continue
            handoff = min(candidates, key=lambda h: abs((idt - h["dt"]).total_seconds()))
            # Record the closest compatible Sonarr search that owns this exact
            # hand-off. This lets presentation distinguish an ordinary later
            # episode search from an automatic retry when deciding whether it may
            # inherit an earlier season batch.
            compatible_starts = [
                st for st in all_starts
                if st["dt"] <= handoff["dt"]
                and handoff["dt"] - st["dt"] <= timedelta(hours=2)
                and arr_media_matches_release("sonarr", st["media_title"], st["season"], st["episode"], title)
            ]
            nearest_start = max(compatible_starts, key=lambda st: st["dt"]) if compatible_starts else None
            if nearest_start is not None:
                item.update({
                    "arr_search_id": nearest_start["event_id"],
                    "arr_search_started": nearest_start["dt"].isoformat(timespec="seconds"),
                    "arr_search_season": nearest_start["season"],
                    "arr_search_episode": nearest_start["episode"],
                    "arr_search_mode": nearest_start["search_mode"],
                })

            matching_sessions = [
                ses for ses in sessions
                if ses["start"] <= handoff["dt"] <= ses["end"] + timedelta(minutes=2)
                and arr_media_matches_release("sonarr", ses["title"], ses["season"], None, title)
            ]
            if not matching_sessions:
                continue
            session = max(matching_sessions, key=lambda ses: ses["start"])

            # Respect a more specific Sonarr search that began after the season
            # search. If S06E03 was explicitly searched while S06 was still open,
            # that hand-off belongs to the episode search rather than the season
            # batch. Automatic retries are allowed to inherit the original batch
            # later in the presentation layer when a directly-linked sibling proves it.
            if nearest_start is not None and nearest_start["event_id"] != session["id"]:
                continue
            used_handoffs.add(handoff["event_id"])
            item.update({
                "arr_handoff_time": handoff["dt"].isoformat(timespec="seconds"),
                "season_search_id": session["id"],
                "season_search_title": session["title"],
                "season_search_season": session["season"],
                "season_search_started": session["start"].isoformat(timespec="seconds"),
                "season_search_completed": session["completed_time"],
                "season_search_reports_downloaded": session["reports_downloaded"],
                "season_search_mode": session["mode"],
                "season_search_confidence": session["confidence"],
            })

    def _attempt_items_for_rows(self, conn: sqlite3.Connection, selected_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Build normal attempt rows for an explicit set of SAB jobs.

        This mirrors the attempt semantics used by ``snapshot`` but keeps the work
        bounded to the requested recent sample plus the six-hour release cluster
        and seven-day attribution look-back required to interpret those attempts.
        """
        if not selected_rows:
            return []

        selected_ids = {str(r["event_id"]) for r in selected_rows}
        selected_times = [self._local_dt(r["event_time"]) for r in selected_rows]
        selected_times = [x for x in selected_times if x is not None]
        earliest = min(selected_times) if selected_times else local_now()
        exact_grabs, episode_grabs = self._grab_indexes(conn, since=earliest - timedelta(hours=self.attribution_hours))

        # Include enough surrounding jobs to determine whether an exact release
        # cluster failed, recovered, completed or was cancelled.  Diagnostics for
        # that same bounded interval are loaded too, because a neighbouring retry
        # can carry the cancellation/failure code that determines cluster outcome.
        cluster_cutoff = (earliest - timedelta(hours=6)).isoformat(timespec="seconds")
        diag_by_event: dict[str, dict[str, Any]] = {}
        if self._table_exists(conn, "sab_attempt_diagnostics"):
            extra = ""
            if self._column_exists(conn, "sab_attempt_diagnostics", "failure_code"):
                extra += ",failure_code"
            if self._column_exists(conn, "sab_attempt_diagnostics", "failure_confidence"):
                extra += ",failure_confidence"
            for d in conn.execute(
                f"""SELECT event_id,failure_stage,failure_reason,lifecycle_json,source,
                           history_id,log_event_count{extra}
                    FROM sab_attempt_diagnostics WHERE final_time >= ?""",
                (cluster_cutoff,),
            ).fetchall():
                try:
                    lifecycle = json.loads(d["lifecycle_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    lifecycle = []
                diag_by_event[str(d["event_id"])] = {
                    "failure_stage": d["failure_stage"],
                    "failure_code": self._row_value(d, "failure_code", None),
                    "failure_confidence": self._row_value(d, "failure_confidence", None),
                    "failure_reason": d["failure_reason"],
                    "source": d["source"],
                    "history_id": d["history_id"],
                    "lifecycle": lifecycle,
                }

        relevant_jobs = conn.execute(
            """SELECT event_id,event_time,job_name,status,max_repair_blocks
               FROM jobs WHERE event_time >= ? ORDER BY event_time""",
            (cluster_cutoff,),
        ).fetchall()

        def effective_status(row: sqlite3.Row) -> str:
            raw = str(row["status"] or "").lower()
            diag = diag_by_event.get(str(row["event_id"]), {})
            if raw in {"cancelled", "canceled", "aborted"}:
                return "cancelled"
            if raw == "failed" and str(diag.get("failure_code") or "").lower() == "cancelled_aborted":
                return "cancelled"
            return raw

        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in relevant_jobs:
            grouped[normalize_identity(row["job_name"])].append(row)

        meta_by_event: dict[str, dict[str, Any]] = {}
        for group in grouped.values():
            current: list[sqlite3.Row] = []
            previous_dt: datetime | None = None
            clusters: list[list[sqlite3.Row]] = []
            for row in sorted(group, key=lambda x: str(x["event_time"] or "")):
                row_dt = self._local_dt(row["event_time"])
                if current and previous_dt is not None and row_dt is not None and row_dt - previous_dt > timedelta(hours=6):
                    clusters.append(current)
                    current = []
                current.append(row)
                if row_dt is not None:
                    previous_dt = row_dt
            if current:
                clusters.append(current)
            for cluster in clusters:
                statuses = [effective_status(x) for x in cluster]
                failed_seen = False
                recovered = False
                for row in cluster:
                    st = effective_status(row)
                    if st == "failed":
                        failed_seen = True
                    elif st == "completed" and failed_seen:
                        recovered = True
                        break
                eventually_completed = "completed" in statuses
                has_failed = "failed" in statuses
                cancelled_only = bool(statuses) and not eventually_completed and not has_failed and "cancelled" in statuses
                outcome = (
                    "recovered" if recovered else
                    "completed" if eventually_completed else
                    "failed" if has_failed else
                    "cancelled" if cancelled_only else "unresolved"
                )
                for row in cluster:
                    meta_by_event[str(row["event_id"])] = {
                        "outcome": outcome,
                        "recovered": recovered,
                        "eventually_completed": eventually_completed,
                    }

        items: list[dict[str, Any]] = []
        for row in selected_rows:
            event_id = str(row["event_id"])
            grab, method = self._attribute(
                exact_grabs, episode_grabs,
                str(row["job_name"]), str(row["event_time"]), self.attribution_hours,
            )
            diag = diag_by_event.get(event_id, {})
            raw_status = str(row["status"] or "").lower()
            status = raw_status
            if raw_status in {"cancelled", "canceled", "aborted"}:
                status = "cancelled"
            elif raw_status == "failed" and str(diag.get("failure_code") or "").lower() == "cancelled_aborted":
                status = "cancelled"
            meta = meta_by_event.get(event_id, {"outcome": status, "recovered": False, "eventually_completed": status == "completed"})
            items.append({
                "event_id": event_id,
                "time": row["event_time"],
                "title": row["job_name"],
                "raw_status": raw_status,
                "status": status,
                "release_outcome": meta["outcome"],
                "release_recovered": bool(meta.get("recovered")),
                "repair_blocks": int(row["max_repair_blocks"] or 0),
                "indexer": grab["indexer"] if grab else "(unattributed)",
                "match_method": method,
                "failure_stage": diag.get("failure_stage"),
                "failure_code": diag.get("failure_code"),
                "failure_confidence": diag.get("failure_confidence"),
                "failure_reason": diag.get("failure_reason"),
                "diagnostic_source": diag.get("source"),
                "history_id": diag.get("history_id"),
                "lifecycle": diag.get("lifecycle", []),
            })
        self._attach_arr_media_to_attempts(conn, items)
        self._attach_sonarr_season_batches(conn, items)
        items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
        return items

    @staticmethod
    def _summarize_attempt_rows(attempts: list[dict[str, Any]]) -> dict[str, Any]:
        by_indexer: dict[str, dict[str, int]] = defaultdict(lambda: {
            "selected": 0, "eligible": 0, "completed": 0, "failed": 0,
            "repaired": 0, "heavy": 0, "failed_recovered": 0,
            "cancelled": 0, "unresolved": 0, "exact": 0, "episode": 0,
        })
        outcome_counts = {"completed": 0, "failed_recovered": 0, "failed_only": 0, "cancelled_other": 0}
        repair_counts = {"zero": 0, "light": 0, "heavy": 0}
        for row in attempts:
            name = str(row.get("indexer") or "(unattributed)")
            vals = by_indexer[name]
            vals["selected"] += 1
            st = str(row.get("status") or "")
            if st in {"completed", "failed"}:
                vals["eligible"] += 1
                vals["completed"] += int(st == "completed")
                vals["failed"] += int(st == "failed")
            elif st == "cancelled":
                vals["cancelled"] += 1
            else:
                vals["unresolved"] += 1
            vals["exact"] += int(row.get("match_method") == "exact")
            vals["episode"] += int(row.get("match_method") == "episode")
            blocks = int(row.get("repair_blocks") or 0)
            vals["repaired"] += int(blocks > 0)
            vals["heavy"] += int(blocks >= 10)
            failed_recovered = st == "failed" and row.get("release_outcome") == "recovered"
            vals["failed_recovered"] += int(failed_recovered)

            if st == "completed":
                outcome_counts["completed"] += 1
            elif failed_recovered:
                outcome_counts["failed_recovered"] += 1
            elif st == "failed":
                outcome_counts["failed_only"] += 1
            else:
                outcome_counts["cancelled_other"] += 1

            repair_counts["zero" if blocks == 0 else "light" if blocks < 10 else "heavy"] += 1

        indexers = []
        for name, vals in sorted(by_indexer.items(), key=lambda kv: kv[0].lower()):
            eligible = int(vals["eligible"])
            selected = int(vals["selected"])
            indexers.append({
                "indexer": name,
                **vals,
                "success": pct(vals["completed"], eligible),
                "repair_pct": pct(vals["repaired"], selected),
                "heavy_pct": pct(vals["heavy"], selected),
            })
        return {"indexers": indexers, "outcomes": outcome_counts, "repair": repair_counts}

    def _provider_sample_from_attempts(self, conn: sqlite3.Connection, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        if not attempts or not self._table_exists(conn, "sab_history"):
            return {"jobs": 0, "completed_jobs": 0, "shared_jobs": 0, "exclusive_jobs": 0, "bytes": 0, "bytes_human": human_bytes(0), "providers": []}

        history_ids = {str(x.get("history_id")) for x in attempts if x.get("history_id")}
        histories: dict[str, sqlite3.Row] = {}
        if history_ids:
            placeholders = ",".join("?" for _ in history_ids)
            for row in conn.execute(
                f"""SELECT history_id,completed_time,job_name,status,stage_log_json
                    FROM sab_history WHERE history_id IN ({placeholders})""",
                tuple(history_ids),
            ).fetchall():
                histories[str(row["history_id"])] = row

        # Fall back conservatively to same-title history nearest the attempt when
        # an old diagnostic row did not retain history_id.
        missing = [x for x in attempts if not x.get("history_id") or str(x.get("history_id")) not in histories]
        fallback_rows: list[sqlite3.Row] = []
        if missing:
            times = [self._local_dt(x.get("time")) for x in missing]
            times = [x for x in times if x is not None]
            if times:
                cutoff = (min(times) - timedelta(hours=2)).isoformat(timespec="seconds")
                fallback_rows = conn.execute(
                    """SELECT history_id,completed_time,job_name,status,stage_log_json
                       FROM sab_history WHERE COALESCE(completed_time,'') >= ? ORDER BY completed_time""",
                    (cutoff,),
                ).fetchall()
        fallback_by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in fallback_rows:
            fallback_by_name[normalize_identity(row["job_name"])].append(row)

        provider_acc: dict[str, dict[str, int]] = defaultdict(lambda: {"bytes": 0, "jobs": 0, "completed_jobs": 0, "exclusive_jobs": 0})
        jobs = completed_jobs = shared_jobs = exclusive_jobs = total_bytes = 0
        used_histories: set[str] = set()
        for attempt in attempts:
            history = histories.get(str(attempt.get("history_id") or ""))
            if history is None:
                adt = self._local_dt(attempt.get("time"))
                best = None
                best_delta = None
                for candidate in fallback_by_name.get(normalize_identity(attempt.get("title") or ""), []):
                    cdt = self._local_dt(candidate["completed_time"])
                    if adt is None or cdt is None:
                        continue
                    delta = abs((cdt - adt).total_seconds())
                    if delta <= 2 * 3600 and (best_delta is None or delta < best_delta):
                        best, best_delta = candidate, delta
                history = best
            if history is None or str(history["history_id"]) in used_histories:
                continue
            usage = {k: int(v) for k, v in extract_server_contributions(history["stage_log_json"]).items() if int(v or 0) > 0}
            if not usage:
                continue
            used_histories.add(str(history["history_id"]))
            jobs += 1
            completed = str(history["status"] or "").lower() == "completed"
            completed_jobs += int(completed)
            shared_jobs += int(len(usage) > 1)
            exclusive_jobs += int(len(usage) == 1)
            for server, used_bytes in usage.items():
                key = str(server).strip().lower()
                vals = provider_acc[key]
                vals["bytes"] += used_bytes
                vals["jobs"] += 1
                vals["completed_jobs"] += int(completed)
                vals["exclusive_jobs"] += int(len(usage) == 1)
                total_bytes += used_bytes

        providers = []
        for name, vals in sorted(provider_acc.items(), key=lambda kv: (-kv[1]["bytes"], kv[0])):
            providers.append({
                "provider": name,
                "provider_label": provider_label(name),
                **vals,
                "bytes_human": human_bytes(vals["bytes"]),
                "share": pct(vals["bytes"], total_bytes, 1) or 0.0,
            })
        return {
            "jobs": jobs,
            "completed_jobs": completed_jobs,
            "shared_jobs": shared_jobs,
            "exclusive_jobs": exclusive_jobs,
            "bytes": total_bytes,
            "bytes_human": human_bytes(total_bytes),
            "providers": providers,
        }

    def _usenet_grab_protocol_filter(self, conn: sqlite3.Connection) -> str:
        if self._table_exists(conn, "prowlarr_indexers") and self._column_exists(conn, "prowlarr_indexers", "protocol"):
            return """AND (h.indexer_id IS NULL OR EXISTS (
                SELECT 1 FROM prowlarr_indexers pi WHERE pi.indexer_id=h.indexer_id
                AND lower(trim(coalesce(pi.protocol,''))) IN ('usenet','nzb','newznab')
            ))"""
        return ""

    def _hydrate_grab_rows(self, conn: sqlite3.Connection, grabs: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Attach a selected set of Prowlarr grab rows to SAB attempts conservatively.

        Matching is one-to-one across the supplied set.  This matters for the new
        per-indexer fixed samples: independently selected grab streams are combined
        before matching so two indexers cannot accidentally claim the same SAB job.
        """
        if not grabs:
            return []

        grabs = sorted(grabs, key=lambda x: str(x["event_time"] or ""), reverse=True)
        grab_times = [self._local_dt(x["event_time"]) for x in grabs]
        grab_times = [x for x in grab_times if x is not None]
        cutoff = (min(grab_times) - timedelta(seconds=10)).isoformat(timespec="seconds") if grab_times else "1970-01-01T00:00:00"
        jobs = conn.execute(
            """SELECT event_id,event_time,job_name,status,max_repair_blocks
               FROM jobs WHERE event_time >= ? ORDER BY event_time""",
            (cutoff,),
        ).fetchall()
        exact_jobs: dict[str, list[sqlite3.Row]] = defaultdict(list)
        episode_jobs: dict[tuple[str, int, int], list[sqlite3.Row]] = defaultdict(list)
        for job in jobs:
            exact_jobs[normalize_match(job["job_name"])].append(job)
            ep = episode_identity(job["job_name"])
            if ep:
                episode_jobs[ep].append(job)

        used: set[str] = set()
        matched_rows: list[sqlite3.Row] = []
        grab_to_job: dict[str, tuple[sqlite3.Row | None, str]] = {}
        # Match oldest to newest so repeated identical grabs consume jobs in order.
        for grab in reversed(grabs):
            gdt = self._local_dt(grab["event_time"])
            title = str(grab["release_title"] or "")
            candidates = exact_jobs.get(normalize_match(title), [])
            method = "exact"
            if not candidates:
                ep = episode_identity(title)
                candidates = episode_jobs.get(ep, []) if ep else []
                method = "episode" if candidates else "unattributed"
            best = None
            best_delta = None
            for job in candidates:
                if str(job["event_id"]) in used:
                    continue
                jdt = self._local_dt(job["event_time"])
                if gdt is None or jdt is None:
                    continue
                delta = (jdt - gdt).total_seconds()
                if -5 <= delta <= 2 * 3600 and (best_delta is None or delta < best_delta):
                    best, best_delta = job, delta
            if best is not None:
                used.add(str(best["event_id"]))
                matched_rows.append(best)
            grab_to_job[str(grab["event_id"])] = (best, method if best is not None else "unattributed")

        attempt_rows = self._attempt_items_for_rows(conn, matched_rows)
        attempt_by_id = {str(x["event_id"]): x for x in attempt_rows}
        out = []
        for grab in grabs:
            job, method = grab_to_job.get(str(grab["event_id"]), (None, "unattributed"))
            attempt = attempt_by_id.get(str(job["event_id"])) if job is not None else None
            try:
                data = json.loads(grab["data_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
            out.append({
                "grab_id": str(grab["event_id"]),
                "time": str(grab["event_time"]),
                "title": str(grab["release_title"] or "(release unavailable)"),
                "indexer": str(grab["indexer_name"] or grab["indexer_id"] or "(unknown)"),
                "source": str(data.get("source") or "") if isinstance(data, dict) else "",
                "method": str(data.get("grabMethod") or "") if isinstance(data, dict) else "",
                "status": attempt.get("status") if attempt else "unresolved",
                "release_outcome": attempt.get("release_outcome") if attempt else "unresolved",
                "repair_blocks": int(attempt.get("repair_blocks") or 0) if attempt else 0,
                "match_method": method,
                "attempt_event_id": attempt.get("event_id") if attempt else None,
            })
        return out

    def _recent_grab_sample(self, conn: sqlite3.Connection, limit: int) -> tuple[list[dict[str, Any]], int]:
        protocol_filter = self._usenet_grab_protocol_filter(conn)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM prowlarr_history h WHERE lower(h.event_type) LIKE '%grab%' {protocol_filter}"
        ).fetchone()[0])
        grabs = conn.execute(
            f"""SELECT h.event_id,h.event_time,h.indexer_id,h.indexer_name,h.release_title,h.data_json
                FROM prowlarr_history h
                WHERE lower(h.event_type) LIKE '%grab%' {protocol_filter}
                ORDER BY h.event_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return self._hydrate_grab_rows(conn, list(grabs)), total

    def _recent_indexer_grab_sample(self, conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
        """Evaluate each indexer on its own newest N grabs.

        This is intentionally different from the combined-grab population.  The
        quality cards answer "how did this indexer's last N selections perform?",
        while the small combined section answers "who supplied the newest N grabs
        overall?".  Unmatched grabs remain unknown rather than becoming failures.
        """
        protocol_filter = self._usenet_grab_protocol_filter(conn)
        rows = conn.execute(
            f"""SELECT h.event_id,h.event_time,h.indexer_id,h.indexer_name,h.release_title,h.data_json
                FROM prowlarr_history h
                WHERE lower(h.event_type) LIKE '%grab%' {protocol_filter}
                ORDER BY h.event_time DESC"""
        ).fetchall()

        total_by: dict[str, int] = defaultdict(int)
        selected_by: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            name = str(row["indexer_name"] or row["indexer_id"] or "(unknown)")
            total_by[name] += 1
            if len(selected_by[name]) < limit:
                selected_by[name].append(row)

        selected_rows = [row for group in selected_by.values() for row in group]
        hydrated = self._hydrate_grab_rows(conn, selected_rows)
        hydrated_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in hydrated:
            hydrated_by[str(row.get("indexer") or "(unknown)")].append(row)

        indexers = []
        all_times = []
        for name in sorted(total_by, key=str.lower):
            items = hydrated_by.get(name, [])
            completed = sum(str(x.get("status") or "") == "completed" for x in items)
            failed = sum(str(x.get("status") or "") == "failed" for x in items)
            linked = sum(bool(x.get("attempt_event_id")) for x in items)
            selected = len(items)
            final_results = completed + failed
            unknown = max(0, selected - final_results)
            unlinked = max(0, selected - linked)
            linked_other = max(0, linked - final_results)
            repaired = sum(int(x.get("repair_blocks") or 0) > 0 for x in items if x.get("attempt_event_id"))
            heavy = sum(int(x.get("repair_blocks") or 0) >= 10 for x in items if x.get("attempt_event_id"))
            times = [str(x.get("time") or "") for x in items if x.get("time")]
            all_times.extend(times)
            indexers.append({
                "indexer": name,
                "requested": limit,
                "selected": selected,
                "total_available": int(total_by.get(name, 0)),
                "linked": linked,
                "completed": completed,
                "failed": failed,
                "unknown": unknown,
                "unlinked": unlinked,
                "linked_other": linked_other,
                "final_results": final_results,
                "success": pct(completed, final_results),
                "repair_pct": pct(repaired, linked),
                "heavy_pct": pct(heavy, linked),
                "span_start": min(times) if times else None,
                "span_end": max(times) if times else None,
            })

        # Keep a separate shared newest-N grab population for selection-share
        # context.  It must not be confused with the independent quality samples.
        combined_grabs, combined_total = self._recent_grab_sample(conn, limit)
        combined_acc: dict[str, int] = defaultdict(int)
        for row in combined_grabs:
            combined_acc[str(row.get("indexer") or "(unknown)")] += 1
        combined = []
        denom = len(combined_grabs)
        for name in sorted(combined_acc, key=str.lower):
            count = combined_acc[name]
            combined.append({"indexer": name, "grabs": count, "share": pct(count, denom)})

        return {
            "mode": "sample",
            "population": "indexer_grabs",
            "requested": limit,
            "selected": limit,
            "total_available": sum(total_by.values()),
            "span_start": min(all_times) if all_times else None,
            "span_end": max(all_times) if all_times else None,
            "indexers": indexers,
            "combined_selected": len(combined_grabs),
            "combined_total_available": combined_total,
            "combined_indexers": combined,
        }

    def _provider_history_sample(self, conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
        """Newest N terminal SAB History rows that actually retain provider usage.

        Provider fixed samples are evidence-specific by design: the denominator is
        downloads for which SAB retained a Servers contribution split, not all SAB
        attempts with rows discarded afterwards.
        """
        if not self._table_exists(conn, "sab_history"):
            return {
                "mode": "sample", "population": "provider_downloads", "requested": limit,
                "selected": 0, "total_available": 0, "span_start": None, "span_end": None,
                "providers": {"jobs": 0, "completed_jobs": 0, "failed_jobs": 0, "shared_jobs": 0,
                              "exclusive_jobs": 0, "bytes": 0, "bytes_human": human_bytes(0), "providers": []},
            }

        rows = conn.execute(
            """SELECT history_id,completed_time,job_name,status,stage_log_json
               FROM sab_history
               WHERE lower(coalesce(status,'')) IN ('completed','failed')
               ORDER BY completed_time DESC"""
        ).fetchall()

        evidenced: list[tuple[sqlite3.Row, dict[str, int]]] = []
        total_available = 0
        for row in rows:
            usage = {k: int(v) for k, v in extract_server_contributions(row["stage_log_json"]).items() if int(v or 0) > 0}
            if not usage:
                continue
            total_available += 1
            if len(evidenced) < limit:
                evidenced.append((row, usage))

        # Stable provider order follows first appearance in provider snapshots;
        # providers found only in old history are appended alphabetically.
        provider_order: dict[str, int] = {}
        provider_labels: dict[str, str] = {}
        if self._table_exists(conn, "provider_snapshots"):
            first_rows = conn.execute(
                """SELECT provider, MIN(id) AS first_id
                   FROM provider_snapshots GROUP BY provider ORDER BY first_id, provider"""
            ).fetchall()
            for pos, row in enumerate(first_rows):
                provider_order[str(row["provider"] or "").strip().lower()] = pos
            latest_rows = conn.execute(
                """SELECT ps.* FROM provider_snapshots ps
                   JOIN (SELECT provider, MAX(id) AS max_id FROM provider_snapshots GROUP BY provider) last
                     ON last.provider=ps.provider AND last.max_id=ps.id"""
            ).fetchall()
            for row in latest_rows:
                key = str(row["provider"] or "").strip().lower()
                stored = str(self._row_value(row, "provider_label", "") or "").strip()
                provider_labels[key] = provider_label(key) if (not stored or "." in stored or stored.lower() == key) else stored

        acc: dict[str, dict[str, int]] = defaultdict(lambda: {
            "bytes": 0, "jobs": 0, "completed_jobs": 0, "failed_jobs": 0, "exclusive_jobs": 0,
        })
        total_bytes = shared_jobs = exclusive_jobs = completed_jobs = failed_jobs = 0
        selected_times = []
        for row, usage in evidenced:
            selected_times.append(str(row["completed_time"] or ""))
            completed = str(row["status"] or "").lower() == "completed"
            completed_jobs += int(completed)
            failed_jobs += int(not completed)
            shared_jobs += int(len(usage) > 1)
            exclusive_jobs += int(len(usage) == 1)
            for server, used_bytes in usage.items():
                key = str(server).strip().lower()
                vals = acc[key]
                vals["bytes"] += int(used_bytes)
                vals["jobs"] += 1
                vals["completed_jobs"] += int(completed)
                vals["failed_jobs"] += int(not completed)
                vals["exclusive_jobs"] += int(len(usage) == 1)
                total_bytes += int(used_bytes)

        missing_order = sorted(k for k in acc if k not in provider_order)
        next_pos = len(provider_order)
        for key in missing_order:
            provider_order[key] = next_pos
            next_pos += 1

        providers = []
        for name in sorted(acc, key=lambda k: (provider_order.get(k, 10**9), k)):
            vals = acc[name]
            providers.append({
                "provider": name,
                "provider_label": provider_labels.get(name, provider_label(name)),
                **vals,
                "bytes_human": human_bytes(vals["bytes"]),
                "share": pct(vals["bytes"], total_bytes, 1) or 0.0,
            })

        selected = len(evidenced)
        pc = {
            "jobs": selected,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "shared_jobs": shared_jobs,
            "exclusive_jobs": exclusive_jobs,
            "bytes": total_bytes,
            "bytes_human": human_bytes(total_bytes),
            "providers": providers,
        }
        selected_times = [x for x in selected_times if x]
        return {
            "mode": "sample", "population": "provider_downloads", "requested": limit,
            "selected": selected, "total_available": total_available,
            "span_start": min(selected_times) if selected_times else None,
            "span_end": max(selected_times) if selected_times else None,
            "providers": pc,
        }

    def sample_snapshot(self, population: str = "downloads", limit: int = 100) -> dict[str, Any]:
        """Count-based analytics independent of elapsed time.

        ``population`` selects a count-based analytical population.  Searches and
        downloads are shared newest-N populations; ``indexer_grabs`` evaluates the
        newest N grabs independently for each indexer; ``provider_downloads`` uses
        only terminal SAB History rows that actually retained provider contribution
        evidence.
        """
        population = str(population or "downloads").lower()
        if population not in {"searches", "grabs", "downloads", "indexer_grabs", "provider_downloads"}:
            population = "downloads"
        limit = int(limit or 100)
        if limit not in {50, 100, 250, 500, 1000}:
            limit = 100

        with self.connect() as conn:
            if population == "indexer_grabs":
                return self._recent_indexer_grab_sample(conn, limit)

            if population == "provider_downloads":
                return self._provider_history_sample(conn, limit)

            if population == "searches":
                coverage, total = self._recent_search_coverage(conn, limit)
                sessions = list(coverage.get("sessions", []))
                times = [str(x.get("time") or "") for x in sessions if x.get("time")]
                return {
                    "mode": "sample", "population": population, "requested": limit,
                    "selected": len(sessions), "total_available": total,
                    "span_start": min(times) if times else None, "span_end": max(times) if times else None,
                    "search_coverage": coverage,
                    "indexers": coverage.get("indexers", []),
                }

            if population == "grabs":
                grabs, total = self._recent_grab_sample(conn, limit)
                summary = self._summarize_attempt_rows(grabs)
                times = [str(x.get("time") or "") for x in grabs if x.get("time")]
                matched_attempts = [x for x in grabs if x.get("attempt_event_id")]

                # A grab sample answers a different question from a download sample:
                # who supplied the releases Arr/Prowlarr selected?  Keep the primary
                # comparison about selection share, with SAB linkage shown only as
                # evidence coverage instead of turning the view into another download
                # success table.
                grab_acc: dict[str, dict[str, int]] = defaultdict(lambda: {"selected": 0, "linked": 0, "exact": 0, "episode": 0})
                for row in grabs:
                    name = str(row.get("indexer") or "(unattributed)")
                    vals = grab_acc[name]
                    vals["selected"] += 1
                    vals["linked"] += int(bool(row.get("attempt_event_id")))
                    vals["exact"] += int(row.get("match_method") == "exact")
                    vals["episode"] += int(row.get("match_method") == "episode")
                denom = max(1, len(grabs))
                grab_indexers = []
                for name, vals in sorted(grab_acc.items(), key=lambda kv: (-kv[1]["selected"], kv[0].lower())):
                    grab_indexers.append({
                        "indexer": name,
                        **vals,
                        "share": pct(vals["selected"], denom),
                        "linked_pct": pct(vals["linked"], vals["selected"]),
                    })

                return {
                    "mode": "sample", "population": population, "requested": limit,
                    "selected": len(grabs), "total_available": total,
                    "span_start": min(times) if times else None, "span_end": max(times) if times else None,
                    "grabs": grabs,
                    "indexers": summary["indexers"], "outcomes": summary["outcomes"], "repair": summary["repair"],
                    "grab_indexers": grab_indexers,
                    "matched_attempts": len(matched_attempts),
                }

            # "Downloads" is a performance population: select the newest N SAB
            # attempts that actually reached a completed/failed terminal result.
            # Queue cancellations and still-unresolved hand-offs are separate
            # operational populations and should not displace a real download from
            # a fixed-count success/failure sample.
            terminal_where = "lower(j.status) IN ('completed','failed')"
            if self._table_exists(conn, "sab_attempt_diagnostics"):
                terminal_where += " AND NOT EXISTS (SELECT 1 FROM sab_attempt_diagnostics d WHERE d.event_id=j.event_id AND lower(coalesce(d.failure_code,''))='cancelled_aborted')"
            total = int(conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {terminal_where}").fetchone()[0]) if self._table_exists(conn, "jobs") else 0
            selected_rows = conn.execute(
                f"""SELECT j.event_id,j.event_time,j.job_name,j.status,j.max_repair_blocks
                    FROM jobs j WHERE {terminal_where}
                    ORDER BY j.event_time DESC LIMIT ?""",
                (limit,),
            ).fetchall() if total else []
            attempts = self._attempt_items_for_rows(conn, list(selected_rows))
            summary = self._summarize_attempt_rows(attempts)
            providers = self._provider_sample_from_attempts(conn, attempts)
            times = [str(x.get("time") or "") for x in attempts if x.get("time")]
            return {
                "mode": "sample", "population": population, "requested": limit,
                "selected": len(attempts), "total_available": total,
                "span_start": min(times) if times else None, "span_end": max(times) if times else None,
                "attempts": attempts,
                "indexers": summary["indexers"], "outcomes": summary["outcomes"], "repair": summary["repair"],
                "providers": providers,
            }


    def activity(self, hours: int = 168) -> dict[str, Any]:
        """Return orchestration activity without treating browsing/cancellation as indexer failure.

        Searches, hand-offs, terminal SAB outcomes and Arr import outcomes are kept as
        distinct populations. Search origin is deliberately conservative: automatic,
        automatic-retry, interactive and RSS labels are only shown when retained Arr
        evidence supports them; otherwise the trigger remains unknown.
        """
        hours = max(1, min(int(hours), 24 * 60))
        with self.connect() as conn:
            now = local_now()
            cutoff = now - timedelta(hours=hours)
            context_cutoff = cutoff - timedelta(minutes=30)
            query_cutoff = context_cutoff.isoformat(timespec="seconds")
            exact_grabs, episode_grabs = self._grab_indexes(
                conn, context_cutoff - timedelta(hours=self.attribution_hours)
            )

            arr_rows = []
            if self._table_exists(conn, "arr_events"):
                has_mode = self._column_exists(conn, "arr_events", "search_mode")
                has_conf = self._column_exists(conn, "arr_events", "origin_confidence")
                mode_sql = "search_mode" if has_mode else "NULL AS search_mode"
                conf_sql = "origin_confidence" if has_conf else "NULL AS origin_confidence"
                rows = conn.execute(
                    f"""SELECT event_id,event_time,app,component,event_type,{mode_sql},{conf_sql},
                               media_title,season,episode,release_name,indexer,download_client,
                               severity,reason,message,details_json
                        FROM arr_events WHERE event_time >= ? ORDER BY event_time""",
                    (query_cutoff,)
                ).fetchall()
                for r in rows:
                    dt = self._local_dt(r["event_time"])
                    if dt is None or dt < cutoff - timedelta(minutes=30):
                        continue
                    try:
                        details = json.loads(r["details_json"] or "{}")
                    except Exception:
                        details = {}
                    if not isinstance(details, dict):
                        details = {}
                    arr_rows.append({
                        "event_id": r["event_id"], "time": r["event_time"], "dt": dt,
                        "app": str(r["app"] or "arr").lower(), "component": r["component"],
                        "event_type": str(r["event_type"] or "event").lower(),
                        "search_mode": str(r["search_mode"] or "").lower() or None,
                        "origin_confidence": str(r["origin_confidence"] or "").lower() or None,
                        "media_title": r["media_title"], "season": r["season"], "episode": r["episode"],
                        "release_name": r["release_name"], "indexer": r["indexer"],
                        "download_client": r["download_client"], "severity": r["severity"],
                        "reason": r["reason"], "message": r["message"], "details": details,
                    })

            # A selected Arr candidate is direct evidence that the search chose a
            # release. Keep that evidence independent of the later Sent-to-SAB
            # correlation window: a hand-off can occur after a search session closes
            # (Tulsa King S01E01 is a retained example), but the search itself must
            # still be shown as "release selected".
            selected_candidates_by_search: dict[str, int] = {}
            if self._table_exists(conn, "arr_search_candidates"):
                for r in conn.execute(
                    """SELECT search_event_id, COUNT(*) AS n
                       FROM arr_search_candidates
                       WHERE event_time >= ? AND decision='selected'
                       GROUP BY search_event_id""",
                    (query_cutoff,)
                ).fetchall():
                    selected_candidates_by_search[str(r["search_event_id"])] = int(r["n"] or 0)

            # Prowlarr query/RSS context used to show which indexers were exercised,
            # including interactive searches where no release was selected.
            prow_context = []
            if self._table_exists(conn, "prowlarr_history"):
                for r in conn.execute(
                    """SELECT event_time,indexer_id,indexer_name,event_type,data_json
                       FROM prowlarr_history WHERE event_time >= ? ORDER BY event_time""",
                    (query_cutoff,)
                ).fetchall():
                    dt = self._local_dt(r["event_time"])
                    if dt is None or dt < cutoff - timedelta(minutes=30):
                        continue
                    et = str(r["event_type"] or "").lower()
                    if "query" not in et and "rss" not in et:
                        continue
                    try:
                        data = json.loads(r["data_json"] or "{}")
                    except Exception:
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    prow_context.append({
                        "time": r["event_time"], "dt": dt,
                        "indexer": str(r["indexer_name"] or r["indexer_id"] or "(unknown)"),
                        "kind": "rss" if "rss" in et else "search",
                        "source": str(data.get("source") or "").strip(),
                        "query": str(data.get("query") or "").strip(),
                        "query_type": str(data.get("queryType") or "").strip(),
                        "results": data.get("queryResults"),
                        "from_log": False,
                    })

            # Rotating Prowlarr logs retain query fan-out even when the API-history
            # snapshot is sparse. Add them as a fallback, suppressing a same-indexer
            # API-history event within two seconds so one query is not shown twice.
            if self._table_exists(conn, "prowlarr_log_searches"):
                # Duplicate suppression used to scan the entire accumulated context
                # for every retained log query. Keep an ordered timestamp index per
                # indexer instead so a +/-2s duplicate check is O(log n).
                context_times_by_indexer: dict[str, list[datetime]] = defaultdict(list)
                for item in prow_context:
                    context_times_by_indexer[str(item.get("indexer") or "(unknown)")].append(item["dt"])
                for values in context_times_by_indexer.values():
                    values.sort()

                for r in conn.execute(
                    """SELECT event_time,indexer_name,query_text
                       FROM prowlarr_log_searches WHERE event_time >= ? ORDER BY event_time""",
                    (query_cutoff,)
                ).fetchall():
                    dt = self._local_dt(r["event_time"])
                    if dt is None or dt < context_cutoff:
                        continue
                    indexer = str(r["indexer_name"] or "(unknown)")
                    timeline = context_times_by_indexer[indexer]
                    left = bisect_left(timeline, dt - timedelta(seconds=2))
                    duplicate = left < len(timeline) and timeline[left] <= dt + timedelta(seconds=2)
                    if duplicate:
                        continue
                    prow_context.append({
                        "time": r["event_time"], "dt": dt, "indexer": indexer,
                        "kind": "search", "source": "", "query": str(r["query_text"] or "").strip(),
                        "query_type": "log", "results": None, "from_log": True,
                    })
                    insort(timeline, dt)
            prow_context.sort(key=lambda x: x["dt"])
            prow_context_times = [x["dt"] for x in prow_context]

            def prow_context_window(start: datetime, end: datetime) -> list[dict[str, Any]]:
                left = bisect_left(prow_context_times, start)
                right = bisect_right(prow_context_times, end)
                return prow_context[left:right]

            diag_by_event = {}
            if self._table_exists(conn, "sab_attempt_diagnostics"):
                cols = "event_id,failure_stage,failure_reason"
                if self._column_exists(conn, "sab_attempt_diagnostics", "failure_code"):
                    cols += ",failure_code"
                for d in conn.execute(f"SELECT {cols} FROM sab_attempt_diagnostics").fetchall():
                    diag_by_event[d["event_id"]] = {
                        "failure_stage": d["failure_stage"],
                        "failure_reason": d["failure_reason"],
                        "failure_code": self._row_value(d, "failure_code", None),
                    }

            jobs = conn.execute(
                "SELECT event_id,event_time,job_name,status,max_repair_blocks FROM jobs WHERE event_time >= ? ORDER BY event_time",
                (query_cutoff,)
            ).fetchall()

            def effective(row):
                raw = str(row["status"] or "").lower()
                d = diag_by_event.get(row["event_id"], {})
                if raw in {"cancelled", "canceled", "aborted"} or (
                    raw == "failed" and str(d.get("failure_code") or "").lower() == "cancelled_aborted"
                ):
                    return "cancelled"
                return raw

            def media_label(a):
                title = str(a.get("media_title") or "Matching media")
                season, episode = a.get("season"), a.get("episode")
                if season is not None:
                    title += f" · S{int(season):02d}"
                    if episode is not None:
                        title += f"E{int(episode):02d}"
                return title

            sent = [a for a in arr_rows if a["event_type"] == "sent_to_sab"]
            search_completed = [a for a in arr_rows if a["event_type"] == "search_completed"]
            rss_started = [a for a in arr_rows if a["event_type"] == "rss_started"]
            rss_completed = [a for a in arr_rows if a["event_type"] == "rss_completed"]
            pre_fail = [a for a in arr_rows if a["event_type"] in {"grab_failed", "grab_error"}]

            # Match each Arr hand-off to one terminal SAB outcome.  Exact release
            # title is strongest.  If Arr/SAB renamed the release, fall back to
            # the same Sonarr episode (or Radarr media title) plus the same
            # attributed indexer.  Terminal rows are consumed one-to-one so an
            # older hand-off cannot incorrectly reuse a later attempt's result.
            terminal_for_sent = {}
            used_terminal_ids: set[str] = set()
            sent_ordered = sorted(sent, key=lambda x: x["dt"])

            for a in sent_ordered:
                if not a.get("release_name"):
                    continue
                key = normalize_match(a["release_name"])
                candidates = []
                for j in jobs:
                    if str(j["event_id"]) in used_terminal_ids:
                        continue
                    if normalize_match(j["job_name"]) != key:
                        continue
                    jdt = self._local_dt(j["event_time"])
                    if jdt is None or jdt < a["dt"] - timedelta(minutes=2):
                        continue
                    if jdt - a["dt"] > timedelta(hours=24):
                        continue
                    candidates.append((jdt, j))
                if candidates:
                    chosen = min(candidates, key=lambda x: x[0])[1]
                    terminal_for_sent[a["event_id"]] = chosen
                    used_terminal_ids.add(str(chosen["event_id"]))

            def handoff_matches_job(a, j) -> bool:
                release_name = str(a.get("release_name") or "")
                job_name = str(j["job_name"] or "")
                if release_name:
                    ae = episode_identity(release_name)
                    je = episode_identity(job_name)
                    if ae is not None and je is not None and ae == je:
                        return True
                return arr_media_matches_release(
                    a.get("app"), a.get("media_title"), a.get("season"), a.get("episode"), job_name
                )

            for a in sent_ordered:
                if a["event_id"] in terminal_for_sent:
                    continue
                candidates = []
                for j in jobs:
                    if str(j["event_id"]) in used_terminal_ids:
                        continue
                    jdt = self._local_dt(j["event_time"])
                    if jdt is None or jdt < a["dt"] - timedelta(minutes=2) or jdt - a["dt"] > timedelta(hours=12):
                        continue
                    if not handoff_matches_job(a, j):
                        continue
                    grab, _method = self._attribute(
                        exact_grabs, episode_grabs, str(j["job_name"]), str(j["event_time"]), self.attribution_hours
                    )
                    if a.get("indexer"):
                        if not grab or str(grab.get("indexer") or "").lower() != str(a.get("indexer") or "").lower():
                            continue
                    candidates.append((abs((jdt - a["dt"]).total_seconds()), jdt, j))
                if candidates:
                    chosen = min(candidates, key=lambda x: (x[0], x[1]))[2]
                    terminal_for_sent[a["event_id"]] = chosen
                    used_terminal_ids.add(str(chosen["event_id"]))

            # Reconcile hand-offs against SAB History too.  History often outlives
            # rotating SAB logs.  Use the same exact-first, semantic-fallback and
            # one-to-one rules before declaring anything unresolved.
            history_for_sent = {}
            if self._table_exists(conn, "sab_history"):
                history_rows = conn.execute(
                    """SELECT history_id,completed_time,job_name,status FROM sab_history
                       WHERE COALESCE(job_name,'')<>'' AND COALESCE(status,'')<>'' AND completed_time >= ?
                       ORDER BY completed_time""",
                    (query_cutoff,)
                ).fetchall()
                used_history_ids: set[str] = set()
                for semantic in (False, True):
                    for a in sent_ordered:
                        if a["event_id"] in terminal_for_sent or a["event_id"] in history_for_sent:
                            continue
                        key = normalize_match(a.get("release_name") or "")
                        candidates = []
                        for h in history_rows:
                            if str(h["history_id"]) in used_history_ids:
                                continue
                            hdt = self._local_dt(h["completed_time"])
                            if hdt is None or hdt < a["dt"] - timedelta(minutes=2) or hdt - a["dt"] > timedelta(days=7):
                                continue
                            if not semantic:
                                if not key or normalize_match(h["job_name"]) != key:
                                    continue
                            else:
                                release_name = str(a.get("release_name") or "")
                                hist_name = str(h["job_name"] or "")
                                same_episode = bool(
                                    release_name and episode_identity(release_name) is not None
                                    and episode_identity(release_name) == episode_identity(hist_name)
                                )
                                same_media = arr_media_matches_release(
                                    a.get("app"), a.get("media_title"), a.get("season"), a.get("episode"), hist_name
                                )
                                if not (same_episode or same_media):
                                    continue
                            candidates.append((hdt, h))
                        if candidates:
                            chosen = min(candidates, key=lambda x: x[0])[1]
                            history_for_sent[a["event_id"]] = chosen
                            used_history_ids.add(str(chosen["history_id"]))

            # Arr can emit duplicate sent-to-SAB evidence for one acquisition.
            # Keep terminal matching one-to-one, but mark a nearby same-release /
            # same-indexer duplicate as explained instead of manufacturing an
            # unresolved hand-off card.
            duplicate_sent: dict[str, str] = {}
            resolved_sent = [
                x for x in sent_ordered
                if x["event_id"] in terminal_for_sent or x["event_id"] in history_for_sent
            ]
            for a in sent_ordered:
                if a["event_id"] in terminal_for_sent or a["event_id"] in history_for_sent:
                    continue
                for b in resolved_sent:
                    if str(a.get("app") or "").lower() != str(b.get("app") or "").lower():
                        continue
                    ai = str(a.get("indexer") or "").strip().lower()
                    bi = str(b.get("indexer") or "").strip().lower()
                    if ai and bi and ai != bi:
                        continue
                    # Duplicate Arr hand-off lines for one acquisition can be
                    # several minutes apart during season/manual workflows.  A
                    # longer window is safe here because we still require the same
                    # application, indexer and exact release/episode identity.
                    if abs((a["dt"] - b["dt"]).total_seconds()) > 15 * 60:
                        continue
                    ar = str(a.get("release_name") or "")
                    br = str(b.get("release_name") or "")
                    same_release = bool(ar and br and normalize_match(ar) == normalize_match(br))
                    same_episode = bool(
                        ar and br and episode_identity(ar) is not None
                        and episode_identity(ar) == episode_identity(br)
                    )
                    if same_release or same_episode:
                        duplicate_sent[a["event_id"]] = b["event_id"]
                        break

            def same_scope(a, b):
                if str(a.get("app") or "").lower() != str(b.get("app") or "").lower():
                    return False
                if normalize_match(a.get("media_title") or "") != normalize_match(b.get("media_title") or ""):
                    return False
                return a.get("season") == b.get("season") and a.get("episode") == b.get("episode")

            search_starts = [x for x in arr_rows if x["event_type"] == "search_started"]

            # Search sessions have explicit ownership windows. This is important for
            # interactive season browsing: a later same-scope search closes the older
            # result page, while an RSS sync closes at its own completion. We therefore
            # never let an unrelated automatic completion retroactively claim a manual
            # search or its grabs.
            searches = []
            for a in [x for x in search_starts if x["dt"] >= cutoff]:
                mode = a.get("search_mode") or "unknown"
                confidence = a.get("origin_confidence") or "none"
                if mode not in {"automatic", "automatic_retry", "rss", "interactive"}:
                    mode, confidence = "unknown", "none"
                elif confidence in {"", "none", None}:
                    confidence = "high" if mode in {"automatic", "automatic_retry", "rss"} else "medium"

                next_same = [x for x in search_starts if x["dt"] > a["dt"] and same_scope(a, x)]
                session_end = min((x["dt"] for x in next_same), default=a["dt"] + timedelta(minutes=30))
                if mode in {"automatic", "automatic_retry"}:
                    matching_done = [
                        x for x in search_completed
                        if x["dt"] >= a["dt"] and x["dt"] <= session_end
                        and same_scope(a, x)
                    ]
                    if matching_done:
                        session_end = min(x["dt"] for x in matching_done)

                ctx = prow_context_window(a["dt"], session_end)
                app_name = a["app"].title()
                ctx = [x for x in ctx if not x["source"] or x["source"].lower() == app_name.lower()] or ctx
                search_ctx = [x for x in ctx if x["kind"] == "search"]
                indexers = sorted({x["indexer"] for x in search_ctx}, key=str.lower)

                related_sent = []
                for x in sent:
                    if x["app"] != a["app"] or not (a["dt"] <= x["dt"] <= session_end):
                        continue
                    if x.get("release_name") and arr_media_matches_release(a["app"], a.get("media_title"), a.get("season"), a.get("episode"), x["release_name"]):
                        related_sent.append(x)
                    elif a.get("episode") is None and x.get("release_name"):
                        related_sent.append(x)
                related_fail = [x for x in pre_fail if x["app"] == a["app"] and a["dt"] <= x["dt"] <= session_end]
                explicit_selected = int(selected_candidates_by_search.get(str(a.get("event_id") or ""), 0) or 0)
                outcome = "release selected" if (explicit_selected > 0 or related_sent) else "pre-sab failed" if related_fail else "no selection"
                searches.append({
                    "kind": "search", "time": a["time"], "dt": a["dt"], "end_dt": session_end,
                    "app": app_name, "origin": mode, "origin_confidence": confidence,
                    "media": media_label(a), "release": None,
                    "indexer": ", ".join(indexers) if indexers else "—", "indexers": indexers,
                    "stage": "Search", "outcome": outcome,
                    "detail": f"{len(indexers)} indexer{'s' if len(indexers) != 1 else ''} queried" if indexers else "Indexer context not retained",
                    "selected_count": max(explicit_selected, len(related_sent)),
                    "explicit_selected_count": explicit_selected,
                })

            # RSS is its own session. The completion line gives a hard upper bound;
            # a manual grab after an RSS run that reported zero grabs must not be
            # attributed to that RSS cycle.
            for a in [x for x in rss_started if x["dt"] >= cutoff]:
                app_name = a["app"].title()
                completions = [x for x in rss_completed if x["app"] == a["app"] and x["dt"] >= a["dt"] and x["dt"] <= a["dt"] + timedelta(minutes=30)]
                done = min(completions, key=lambda x: x["dt"]) if completions else None
                session_end = done["dt"] if done else a["dt"] + timedelta(minutes=15)
                ctx = prow_context_window(a["dt"], session_end)
                ctx = [x for x in ctx if not x["source"] or x["source"].lower() == app_name.lower()] or ctx
                indexers = sorted({x["indexer"] for x in ctx}, key=str.lower)
                related_sent = [x for x in sent if x["app"] == a["app"] and a["dt"] <= x["dt"] <= session_end]
                grabbed = None
                found = None
                if done:
                    grabbed = done.get("details", {}).get("reports_grabbed")
                    found = done.get("details", {}).get("reports_found")
                if grabbed is not None:
                    outcome = "release selected" if int(grabbed or 0) > 0 else "no selection"
                else:
                    outcome = "release selected" if related_sent else "no selection"
                detail_parts = []
                if found is not None:
                    detail_parts.append(f"{found} reports found")
                if grabbed is not None:
                    detail_parts.append(f"{grabbed} grabbed")
                if not detail_parts:
                    detail_parts.append(f"{len(indexers)} indexer{'s' if len(indexers) != 1 else ''} exercised" if indexers else "RSS sync observed")
                searches.append({
                    "kind": "search", "time": a["time"], "dt": a["dt"], "end_dt": session_end,
                    "app": app_name, "origin": "rss", "origin_confidence": "high", "media": "RSS sync",
                    "release": None, "indexer": ", ".join(indexers) if indexers else "—", "indexers": indexers,
                    "stage": "RSS", "outcome": outcome, "detail": " · ".join(detail_parts),
                    "selected_count": int(grabbed) if grabbed is not None else len(related_sent),
                    "reported_grabbed": int(grabbed) if grabbed is not None else None,
                })

            def _session_owns_release(session, release_name):
                if session.get("origin") == "rss":
                    return True
                media = str(session.get("media") or "")
                # Recover the original search identity from the display label.
                # The actual Arr start row is found by timestamp below when needed.
                source = next((x for x in search_starts if x.get("time") == session.get("time") and x.get("app", "").title() == session.get("app")), None)
                if source is None:
                    return True
                if arr_media_matches_release(source.get("app"), source.get("media_title"), source.get("season"), source.get("episode"), release_name):
                    return True
                return source.get("episode") is None and bool(release_name)

            # Assign each hand-off/pre-SAB event to only the most specific/latest
            # active search session. This prevents a broad season search from also
            # claiming later episode-specific retries that have their own search.
            for session in searches:
                if session.get("origin") != "rss":
                    explicit_selected = int(session.get("explicit_selected_count") or 0)
                    session["selected_count"] = explicit_selected
                    session["outcome"] = "release selected" if explicit_selected > 0 else "no selection"
            for x in sent:
                candidates = [
                    session for session in searches
                    if str(session.get("app") or "").lower() == str(x.get("app") or "").title().lower()
                    and session.get("dt") <= x["dt"] <= session.get("end_dt")
                    and _session_owns_release(session, x.get("release_name") or "")
                ]
                if candidates:
                    owner = max(candidates, key=lambda z: z["dt"])
                    if owner.get("origin") != "rss" or owner.get("reported_grabbed") is None:
                        if int(owner.get("explicit_selected_count") or 0) > 0:
                            owner["selected_count"] = max(
                                int(owner.get("selected_count") or 0),
                                int(owner.get("explicit_selected_count") or 0),
                            )
                        else:
                            owner["selected_count"] = int(owner.get("selected_count") or 0) + 1
                        owner["outcome"] = "release selected"
            for x in pre_fail:
                candidates = [
                    session for session in searches
                    if str(session.get("app") or "").lower() == str(x.get("app") or "").title().lower()
                    and session.get("dt") <= x["dt"] <= session.get("end_dt")
                    and _session_owns_release(session, x.get("release_name") or "")
                ]
                if candidates:
                    owner = max(candidates, key=lambda z: z["dt"])
                    if owner.get("outcome") == "no selection":
                        owner["outcome"] = "pre-sab failed"

            def nearest_search_session(app_value: str, when: datetime, minutes: int = 45, release_name: str | None = None):
                candidates = [
                    x for x in searches
                    if str(x.get("app") or "").lower() == str(app_value or "").lower()
                    and x.get("dt") is not None and x["dt"] <= when
                    and when <= (x.get("end_dt") or (x["dt"] + timedelta(minutes=minutes)))
                    and (when - x["dt"]) <= timedelta(minutes=minutes)
                    and (not release_name or _session_owns_release(x, release_name))
                ]
                if not candidates:
                    return None
                # A concrete Arr search/retry is more specific evidence than a broad
                # overlapping RSS cycle. Prefer it even when RSS started later. This
                # prevents a zero-grab RSS sync from stealing a Tulsa retry hand-off
                # and its downstream SAB/import activity. RSS remains the owner when
                # no compatible non-RSS search session exists.
                specific = [x for x in candidates if x.get("origin") != "rss"]
                pool = specific or candidates
                return max(pool, key=lambda x: x["dt"])

            sent_for_job_event = {}
            for sent_id, job_row in terminal_for_sent.items():
                sent_row = next((x for x in sent if x["event_id"] == sent_id), None)
                if sent_row is not None:
                    sent_for_job_event[str(job_row["event_id"])] = sent_row

            handoffs = []
            unresolved = []
            for a in [x for x in sent if x["dt"] >= cutoff]:
                j = terminal_for_sent.get(a["event_id"])
                h = history_for_sent.get(a["event_id"])
                if j is not None:
                    st = effective(j)
                    outcome = st
                    detail = f"SAB terminal result: {st}"
                elif h is not None:
                    raw_status = str(h["status"] or "").lower()
                    st = "completed" if raw_status in {"completed", "success", "succeeded"} else "failed" if raw_status in {"failed", "failure"} else raw_status or "resolved"
                    outcome = st
                    detail = f"SAB History terminal result: {st}"
                elif a["event_id"] in duplicate_sent:
                    st = "resolved"
                    outcome = "resolved duplicate"
                    detail = "Duplicate Arr hand-off evidence; terminal result is attached to a nearby same-release hand-off"
                else:
                    age = now - a["dt"]
                    st = "pending" if age < timedelta(hours=2) else "unresolved"
                    outcome = st
                    detail = "No terminal SAB result observed yet" if st == "pending" else "No terminal SAB result retained after full jobs + History reconciliation"
                session = nearest_search_session(a["app"], a["dt"], release_name=a.get("release_name") or "")
                row = {
                    "kind": "handoff", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": (session or {}).get("origin") or "handoff",
                    "origin_confidence": (session or {}).get("origin_confidence") or a.get("origin_confidence") or "high",
                    "media": a.get("media_title") or (session or {}).get("media") or "—", "release": a.get("release_name") or "—",
                    "indexer": a.get("indexer") or "(unknown)", "stage": "Sent to SAB",
                    "outcome": outcome, "detail": detail,
                }
                handoffs.append(row)
                if st == "unresolved":
                    unresolved.append({**row, "kind": "unresolved", "stage": "Unresolved hand-off"})

            downloads = []
            for j in jobs:
                dt = self._local_dt(j["event_time"])
                if dt is None or dt < cutoff:
                    continue
                st = effective(j)
                grab, method = self._attribute(exact_grabs, episode_grabs, str(j["job_name"]), str(j["event_time"]), self.attribution_hours)
                d = diag_by_event.get(j["event_id"], {})
                sent_source = sent_for_job_event.get(str(j["event_id"]))
                source_session = nearest_search_session(sent_source["app"], sent_source["dt"], release_name=sent_source.get("release_name") or "") if sent_source else None
                downloads.append({
                    "kind": "download", "time": j["event_time"], "dt": dt, "app": "SABnzbd",
                    "origin": (source_session or {}).get("origin") or "download",
                    "origin_confidence": (source_session or {}).get("origin_confidence") or method,
                    "media": (source_session or {}).get("media") or "—", "release": j["job_name"],
                    "indexer": grab["indexer"] if grab else "(unattributed)", "stage": "Download",
                    "outcome": st, "detail": (
                        "User/system cancellation or abort; excluded from indexer quality"
                        if st == "cancelled" else d.get("failure_reason") or f"{int(j['max_repair_blocks'] or 0)} repair blocks"
                    ),
                    "failure_stage": d.get("failure_stage"),
                })

            imports = []
            for a in [x for x in arr_rows if x["dt"] >= cutoff and x["event_type"] in {"import_succeeded", "import_failed", "import_cancelled"}]:
                outcome = {"import_succeeded": "imported", "import_failed": "failed", "import_cancelled": "cancelled"}[a["event_type"]]
                prior_sent = []
                for sx in sent:
                    if sx["app"] != a["app"] or sx["dt"] > a["dt"] or (a["dt"] - sx["dt"]) > timedelta(hours=3):
                        continue
                    if a.get("release_name") and sx.get("release_name"):
                        if normalize_match(a["release_name"]) != normalize_match(sx["release_name"]):
                            continue
                    elif a.get("media_title") and sx.get("release_name"):
                        if not arr_media_matches_release(a["app"], a.get("media_title"), a.get("season"), a.get("episode"), sx["release_name"]):
                            continue
                    prior_sent.append(sx)
                source_sent = max(prior_sent, key=lambda x: x["dt"]) if prior_sent else None
                source_session = nearest_search_session(source_sent["app"], source_sent["dt"], release_name=source_sent.get("release_name") or "") if source_sent else None
                imports.append({
                    "kind": "import", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": (source_session or {}).get("origin") or "import",
                    "origin_confidence": (source_session or {}).get("origin_confidence") or "high",
                    "media": media_label(a), "release": a.get("release_name") or (source_sent or {}).get("release_name") or "—",
                    "indexer": a.get("indexer") or (source_sent or {}).get("indexer") or "—", "stage": "Import", "outcome": outcome,
                    "detail": a.get("reason") or a.get("message") or outcome,
                })

            pre_sab = []
            seen_pre = set()
            high_level_pre = [x for x in pre_fail if x["event_type"] == "grab_failed"]
            for a in [x for x in pre_fail if x["dt"] >= cutoff]:
                # The Arr logs often emit a low-level grab_error immediately
                # followed by the more useful grab_failed line. Keep one human
                # event rather than counting the same acquisition failure twice.
                if a["event_type"] == "grab_error" and any(
                    normalize_match(f.get("release_name") or "") == normalize_match(a.get("release_name") or "")
                    and (not a.get("indexer") or not f.get("indexer") or a.get("indexer") == f.get("indexer"))
                    and 0 <= (f["dt"] - a["dt"]).total_seconds() <= 90
                    for f in high_level_pre
                ):
                    continue
                key = (normalize_match(a.get("release_name") or ""), a.get("indexer") or "")
                if key in seen_pre:
                    continue
                seen_pre.add(key)
                source_session = nearest_search_session(a["app"], a["dt"], release_name=a.get("release_name") or "")
                pre_sab.append({
                    "kind": "pre_sab", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": (source_session or {}).get("origin") or "acquisition",
                    "origin_confidence": (source_session or {}).get("origin_confidence") or "high",
                    "media": a.get("media_title") or (source_session or {}).get("media") or "—", "release": a.get("release_name") or "—",
                    "indexer": a.get("indexer") or "(unknown)", "stage": "Before SAB", "outcome": "failed",
                    "detail": a.get("reason") or a.get("message") or "Release acquisition failed before SAB",
                })

            infrastructure = []
            for a in [x for x in arr_rows if x["dt"] >= cutoff and x["event_type"] == "sab_unreachable"]:
                infrastructure.append({
                    "kind": "infrastructure", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": "system", "origin_confidence": "high",
                    "media": a.get("media_title") or "SABnzbd availability", "release": a.get("release_name") or "—",
                    "indexer": "—", "stage": "Download client availability", "outcome": "unavailable",
                    "detail": a.get("reason") or a.get("message") or "Arr could not reach SABnzbd",
                })

            history = []
            if self._table_exists(conn, "sab_log_events"):
                for r in conn.execute(
                    """SELECT event_time,job_name,kind,message,details_json
                       FROM sab_log_events
                       WHERE kind IN ('history_archived','history_removed') AND event_time >= ?
                       ORDER BY event_time""",
                    (query_cutoff,)
                ).fetchall():
                    dt = self._local_dt(r["event_time"])
                    if dt is None or dt < cutoff:
                        continue
                    try:
                        details = json.loads(r["details_json"] or "{}")
                    except Exception:
                        details = {}
                    if not isinstance(details, dict):
                        details = {}
                    outcome = "archived" if r["kind"] == "history_archived" else "removed"
                    release = str(r["job_name"] or "").strip() or "—"
                    nzo = str(details.get("nzo_id") or details.get("history_id") or "").strip()
                    history.append({
                        "kind": "history", "time": r["event_time"], "dt": dt, "app": "SABnzbd",
                        "origin": "system", "origin_confidence": "high", "media": "SAB history",
                        "release": release, "indexer": "—", "stage": "SAB history", "outcome": outcome,
                        "detail": r["message"] or (f"History item {nzo} {outcome}" if nzo else f"History item {outcome}"),
                    })

            # Arr debug logs can say why SAB history disappeared. Keep that separate
            # from SAB queue cancellation: completed-import cleanup and failed-download
            # cleanup are history actions, not indexer-quality outcomes.
            for a in [x for x in arr_rows if x["dt"] >= cutoff and x["event_type"] == "history_removed"]:
                removal_reason = str((a.get("details") or {}).get("removal_reason") or a.get("reason") or "unknown_removal")
                if removal_reason == "arr_remove_completed_history":
                    detail = "Arr removed the completed item from SAB history after import"
                elif removal_reason == "arr_remove_failed_history":
                    detail = "Arr removed the failed item from SAB history before/while redownloading"
                else:
                    detail = a.get("message") or "Arr removed an item from SAB history"
                history.append({
                    "kind": "history", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": "system", "origin_confidence": "high", "media": "SAB history",
                    "release": a.get("release_name") or "—", "indexer": "—",
                    "stage": "SAB history", "outcome": "removed", "detail": detail,
                    "removal_reason": removal_reason,
                })

            cancelled = [dict(x) for x in downloads + imports if x["outcome"] == "cancelled"]
            # Retain explicit Arr-side removal/cancellation events even when no SAB
            # terminal record survived. Suppress near-identical duplicates when the
            # same release is already represented by a cancelled SAB job.
            for a in [x for x in arr_rows if x["dt"] >= cutoff and x["event_type"] == "download_cancelled"]:
                duplicate = any(
                    a.get("release_name") and x.get("release") and normalize_match(a["release_name"]) == normalize_match(x["release"])
                    and abs((self._local_dt(x["time"]) - a["dt"]).total_seconds()) <= 300
                    for x in cancelled if self._local_dt(x.get("time")) is not None
                )
                if duplicate:
                    continue
                cancelled.append({
                    "kind": "download", "time": a["time"], "dt": a["dt"], "app": a["app"].title(),
                    "origin": "download", "origin_confidence": "high",
                    "media": media_label(a), "release": a.get("release_name") or "—",
                    "indexer": a.get("indexer") or "—", "stage": "Download", "outcome": "cancelled",
                    "detail": a.get("reason") or a.get("message") or "Arr recorded a download cancellation/removal; excluded from indexer quality",
                })

            # Routine RSS sync summaries are intentionally not emitted into the Activity
            # feed. They remain useful internally for assigning an RSS trigger to a
            # subsequent hand-off, and Prowlarr's own RSS counters remain on Indexers.
            visible_searches = [dict(x) for x in searches if x.get("origin") != "rss"]
            overview = [dict(x) for x in visible_searches + handoffs + downloads + imports + pre_sab + infrastructure]
            for collection in (visible_searches, handoffs, downloads, imports, unresolved, pre_sab, infrastructure, history, cancelled, overview):
                collection.sort(key=lambda x: x["dt"], reverse=True)
                for x in collection:
                    x.pop("dt", None)
                    x.pop("end_dt", None)
                    x.pop("reported_grabbed", None)

            mode_counts = defaultdict(int)
            for x in searches:
                mode_counts[x["origin"]] += 1
            return {
                "hours": hours,
                "summary": {
                    "searches": len(visible_searches), "interactive": mode_counts.get("interactive", 0),
                    "automatic": mode_counts.get("automatic", 0),
                    "automatic_retry": mode_counts.get("automatic_retry", 0),
                    "unknown": mode_counts.get("unknown", 0), "rss": mode_counts.get("rss", 0),
                    "no_selection": sum(x["outcome"] == "no selection" for x in searches),
                    "handoffs": len(handoffs), "downloads": len(downloads), "imports": len(imports),
                    "cancelled": len(cancelled), "unresolved": len(unresolved), "pre_sab_failed": len(pre_sab),
                    "infrastructure": len(infrastructure), "history_actions": len(history),
                },
                "overview": overview, "searches": visible_searches, "handoffs": handoffs,
                "downloads": downloads, "imports": imports, "unresolved": unresolved,
                "pre_sab": pre_sab, "infrastructure": infrastructure, "history": history, "cancelled": cancelled,
            }

    def release_chain(
        self, title: str, attribution_hours: int | None = None, anchor_time: str | None = None
    ) -> dict[str, Any]:
        """Build a best-effort Prowlarr -> SAB event chain for one release.

        SAB history diagnostics are joined by normalized release title and the
        nearest completion timestamp.  Prowlarr search/RSS context remains a
        best-effort correlation, not a guaranteed causal relationship.
        """
        attribution_hours = self.attribution_hours if attribution_hours is None else attribution_hours
        with self.connect() as conn:
            exact_grabs, episode_grabs = self._grab_indexes(conn)
            ident = normalize_identity(title)
            norm = normalize_match(title)
            ep = episode_identity(title)

            jobs = conn.execute(
                """
                SELECT event_id,event_time,job_name,status,max_repair_blocks
                FROM jobs
                ORDER BY event_time
                """
            ).fetchall()
            selected_jobs = [r for r in jobs if normalize_identity(r["job_name"]) == ident]
            if not selected_jobs:
                selected_jobs = [r for r in jobs if normalize_match(r["job_name"]) == norm]

            # The same exact NZB/release can be downloaded again days later.  Those
            # are separate acquisitions, not one giant retry chain.  Split exact-title
            # observations into contiguous clusters and anchor the drawer to the row
            # the user clicked when a timestamp is supplied; otherwise show the most
            # recent cluster.  Six hours is deliberately generous for genuine retry /
            # redownload flows while keeping multi-day repeats (for example Vikings)
            # separate.
            if selected_jobs:
                selected_jobs = sorted(selected_jobs, key=lambda r: str(r["event_time"] or ""))
                clusters: list[list[Any]] = []
                current: list[Any] = []
                previous_dt = None
                for row in selected_jobs:
                    row_dt = self._local_dt(row["event_time"])
                    if current and previous_dt is not None and row_dt is not None and row_dt - previous_dt > timedelta(hours=6):
                        clusters.append(current)
                        current = []
                    current.append(row)
                    if row_dt is not None:
                        previous_dt = row_dt
                if current:
                    clusters.append(current)
                if len(clusters) > 1:
                    anchor_dt = self._local_dt(anchor_time) if anchor_time else None
                    if anchor_dt is not None:
                        def cluster_distance(cluster):
                            dts = [self._local_dt(r["event_time"]) for r in cluster]
                            dts = [dt for dt in dts if dt is not None]
                            return min((abs((dt - anchor_dt).total_seconds()) for dt in dts), default=float("inf"))
                        selected_jobs = min(clusters, key=cluster_distance)
                    else:
                        selected_jobs = clusters[-1]

            # Release drawers are acquisition stories, not merely exact-NZB stories.
            # When the selected title is an episode, include neighboring attempts for
            # the same SxxExx even when Arr chose a differently named release after a
            # failure. This is what turns e.g. Tulsa King S01E01 into the real
            # fail -> fail -> complete chain instead of hiding the first failed NZB.
            if ep is not None and selected_jobs:
                anchors = [self._local_dt(r["event_time"]) for r in selected_jobs]
                anchors = [dt for dt in anchors if dt is not None]
                if anchors:
                    lo = min(anchors) - timedelta(minutes=30)
                    hi = max(anchors) + timedelta(minutes=30)
                    sibling_jobs = [
                        r for r in jobs
                        if episode_identity(str(r["job_name"])) == ep
                        and (dt := self._local_dt(r["event_time"])) is not None
                        and lo <= dt <= hi
                    ]
                    by_event = {str(r["event_id"]): r for r in selected_jobs}
                    for r in sibling_jobs:
                        by_event[str(r["event_id"])] = r
                    selected_jobs = sorted(by_event.values(), key=lambda r: str(r["event_time"] or ""))

            attempts = []
            for r in selected_jobs:
                grab, match_method = self._attribute(
                    exact_grabs, episode_grabs,
                    str(r["job_name"]), str(r["event_time"]), attribution_hours,
                )
                attempts.append({
                    "event_id": r["event_id"],
                    "time": r["event_time"],
                    "title": r["job_name"],
                    "status": str(r["status"]).lower(),
                    "repair_blocks": int(r["max_repair_blocks"] or 0),
                    "indexer": grab["indexer"] if grab else "(unattributed)",
                    "match_method": match_method,
                    "grab_time": grab["time"] if grab else None,
                    "sab_history": None,
                    "sab_diagnostic": None,
                    "next_grab": None,
                })

            rows = conn.execute(
                """
                SELECT event_id,event_time,indexer_id,indexer_name,event_type,
                       release_title,data_json
                FROM prowlarr_history
                ORDER BY event_time
                """
            ).fetchall()

            contexts = defaultdict(list)
            matching_grabs = []
            for r in rows:
                dt = self._local_dt(r["event_time"])
                if dt is None:
                    continue
                event_type = str(r["event_type"] or "").lower()
                name = str(r["indexer_name"] or r["indexer_id"] or "(unknown)")
                try:
                    data = json.loads(r["data_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}

                if "grab" in event_type and r["release_title"]:
                    exact = normalize_match(r["release_title"]) == norm
                    episode_match = (not exact and ep is not None and episode_identity(r["release_title"]) == ep)
                    if exact or episode_match:
                        matching_grabs.append({
                            "event_id": r["event_id"],
                            "time": r["event_time"],
                            "dt": dt,
                            "indexer": name,
                            "title": r["release_title"],
                            "match_method": "exact" if exact else "episode",
                            "grab_method": str(data.get("grabMethod") or "").strip(),
                            "source": str(data.get("source") or "").strip(),
                            "host": str(data.get("host") or "").strip(),
                        })
                elif "query" in event_type or "rss" in event_type:
                    contexts[name].append({
                        "event_id": r["event_id"],
                        "time": r["event_time"],
                        "dt": dt,
                        "indexer": name,
                        "kind": "rss" if "rss" in event_type else "search",
                        "query": str(data.get("query") or "").strip(),
                        "query_type": str(data.get("queryType") or "").strip(),
                        "results": data.get("queryResults"),
                        "source": str(data.get("source") or "").strip(),
                    })

            # Sonarr/Radarr orchestration events from v7.8 rotating logs.
            matching_arr = []
            if self._table_exists(conn, "arr_events"):
                mode_sql = "search_mode" if self._column_exists(conn, "arr_events", "search_mode") else "NULL AS search_mode"
                conf_sql = "origin_confidence" if self._column_exists(conn, "arr_events", "origin_confidence") else "NULL AS origin_confidence"
                arows = conn.execute(
                    f"""
                    SELECT event_id,event_time,app,component,event_type,{mode_sql},{conf_sql},media_title,
                           season,episode,release_name,indexer,download_client,severity,
                           reason,message,details_json
                    FROM arr_events
                    ORDER BY event_time
                    """
                ).fetchall()
                for r in arows:
                    dt = self._local_dt(r["event_time"])
                    if dt is None:
                        continue
                    release_ok, release_method = arr_release_matches(r["release_name"], title)
                    media_ok = arr_media_matches_release(
                        r["app"], r["media_title"], r["season"], r["episode"], title
                    )
                    # If Arr retained the exact release name, never fall back to
                    # a broad movie/episode media match for a different release.
                    if r["release_name"]:
                        if not release_ok:
                            continue
                    elif not media_ok:
                        continue

                    # Keep every Arr event — including exact release-name hand-offs —
                    # inside the selected acquisition cluster.  The same exact NZB can
                    # be downloaded again days later; without this bound, an old sent
                    # event can reappear as an unresolved hand-off in today's drawer.
                    if attempts:
                        attempt_dts = [self._local_dt(a["time"]) for a in attempts]
                        attempt_dts = [x for x in attempt_dts if x is not None]
                        if attempt_dts and min(abs((dt - x).total_seconds()) for x in attempt_dts) > 6 * 3600:
                            continue
                    try:
                        details = json.loads(r["details_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        details = {}
                    if not isinstance(details, dict):
                        details = {}
                    matching_arr.append({
                        "event_id": r["event_id"],
                        "time": r["event_time"],
                        "dt": dt,
                        "app": str(r["app"] or "arr").lower(),
                        "component": r["component"],
                        "event_type": str(r["event_type"] or "event").lower(),
                        "search_mode": str(r["search_mode"] or "").lower() or None,
                        "origin_confidence": str(r["origin_confidence"] or "").lower() or None,
                        "media_title": r["media_title"],
                        "season": r["season"],
                        "episode": r["episode"],
                        "release_name": r["release_name"],
                        "indexer": r["indexer"],
                        "download_client": r["download_client"],
                        "severity": r["severity"],
                        "reason": r["reason"],
                        "message": r["message"],
                        "details": details,
                        "match_method": release_method if release_ok else "media",
                    })

            # Reconstructed lifecycle from rotating SAB logs.
            diag_by_event = {}
            if self._table_exists(conn, "sab_attempt_diagnostics"):
                diag_extra = ""
                if self._column_exists(conn, "sab_attempt_diagnostics", "failure_code"):
                    diag_extra += ",failure_code"
                if self._column_exists(conn, "sab_attempt_diagnostics", "failure_confidence"):
                    diag_extra += ",failure_confidence"
                for d in conn.execute(
                    f"""SELECT event_id,failure_stage,failure_reason,lifecycle_json,source,
                              history_id,log_event_count{diag_extra}
                       FROM sab_attempt_diagnostics"""
                ).fetchall():
                    try:
                        lifecycle = json.loads(d["lifecycle_json"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        lifecycle = []
                    diag_by_event[d["event_id"]] = {
                        "failure_stage": d["failure_stage"],
                        "failure_code": self._row_value(d, "failure_code", None),
                        "failure_confidence": self._row_value(d, "failure_confidence", None),
                        "failure_reason": d["failure_reason"],
                        "lifecycle": lifecycle if isinstance(lifecycle, list) else [],
                        "source": d["source"] or "inferred",
                        "history_id": d["history_id"],
                        "log_event_count": int(d["log_event_count"] or 0),
                    }
            for attempt in attempts:
                attempt["sab_diagnostic"] = diag_by_event.get(attempt["event_id"])
                if attempt["status"] == "failed" and str((attempt["sab_diagnostic"] or {}).get("failure_code") or "").lower() == "cancelled_aborted":
                    attempt["raw_status"] = "failed"
                    attempt["status"] = "cancelled"
                if attempt["status"] == "completed" and attempt["sab_diagnostic"]:
                    for stage in attempt["sab_diagnostic"].get("lifecycle", []):
                        events = stage.get("events", []) if isinstance(stage, dict) else []
                        only_benign_no_par = bool(events) and all(
                            str(ev.get("kind") or "") == "no_par2" for ev in events if isinstance(ev, dict)
                        )
                        if only_benign_no_par:
                            stage["status"] = "info"
                            for ev in events:
                                if isinstance(ev, dict):
                                    ev["status"] = "info"

            # SAB history: fail_message + per-stage action log (structured enrichment).
            sab_hist = []
            if self._table_exists(conn, "sab_history"):
                hrows = conn.execute(
                    """
                    SELECT history_id,nzo_id,completed_time,job_name,status,fail_message,
                           stage_log_json,download_time,postproc_time,archive
                    FROM sab_history
                    ORDER BY completed_time
                    """
                ).fetchall()
                for h in hrows:
                    same = normalize_identity(h["job_name"]) == ident or normalize_match(h["job_name"]) == norm
                    if not same and ep is not None:
                        same = episode_identity(str(h["job_name"])) == ep
                    if not same:
                        continue
                    stages = self._normalize_stage_log(h["stage_log_json"])
                    fail_message = self._clean_stage_action(h["fail_message"])
                    sab_hist.append({
                        "history_id": h["history_id"],
                        "nzo_id": h["nzo_id"],
                        "time": h["completed_time"],
                        "dt": self._local_dt(h["completed_time"]),
                        "title": h["job_name"],
                        "status": str(h["status"] or "").lower(),
                        "fail_message": fail_message,
                        "failure_stage": self._infer_failure_stage(fail_message, stages),
                        "stages": stages,
                        "download_time": int(h["download_time"] or 0),
                        "postproc_time": int(h["postproc_time"] or 0),
                        "archive": bool(h["archive"]),
                    })

            # Match each attempt to the nearest SAB history row, favoring same status.
            used_history = set()
            for attempt in attempts:
                adt = self._local_dt(attempt["time"])
                best = None
                best_score = None
                for h in sab_hist:
                    if h["history_id"] in used_history or adt is None or h["dt"] is None:
                        continue
                    distance = abs((h["dt"] - adt).total_seconds())
                    status_penalty = 0 if h["status"] == attempt["status"] else 600
                    score = distance + status_penalty
                    if distance <= 3600 and (best_score is None or score < best_score):
                        best, best_score = h, score
                if best:
                    used_history.add(best["history_id"])
                    attempt["sab_history"] = {k: v for k, v in best.items() if k != "dt"}

                # Exact Arr hand-off gives us the orchestration app and a stronger
                # release->SAB correlation than timing alone.
                attempt["arr_handoff"] = None
                attempt["retry_observation"] = None
                if adt is not None:
                    prior_sent = [
                        a for a in matching_arr
                        if a["event_type"] == "sent_to_sab" and a["dt"] <= adt
                        and (adt - a["dt"]) <= timedelta(minutes=30)
                    ]
                    if prior_sent:
                        ah = max(prior_sent, key=lambda x: x["dt"])
                        attempt["arr_handoff"] = {
                            "app": ah["app"], "time": ah["time"],
                            "indexer": ah["indexer"], "match_method": ah["match_method"],
                            "delay_seconds": int((adt - ah["dt"]).total_seconds()),
                        }
                        if item_indexer := ah.get("indexer"):
                            if attempt["indexer"] == "(unattributed)":
                                attempt["indexer"] = item_indexer
                                attempt["match_method"] = "arr-exact" if ah.get("match_method") == "exact" else "arr-episode"

                if attempt["status"] == "failed" and adt is not None:
                    later_searches = [
                        a for a in matching_arr
                        if a["event_type"] == "search_started" and a["dt"] > adt
                        and (a["dt"] - adt) <= timedelta(minutes=20)
                    ]
                    later_sends = [
                        a for a in matching_arr
                        if a["event_type"] == "sent_to_sab" and a["dt"] > adt
                        and (a["dt"] - adt) <= timedelta(minutes=30)
                    ]
                    if later_searches or later_sends:
                        search = min(later_searches, key=lambda x: x["dt"]) if later_searches else None
                        sent = min(later_sends, key=lambda x: x["dt"]) if later_sends else None
                        trigger = search or sent
                        explicit_retry = [
                            a for a in matching_arr
                            if a["event_type"] == "retry_search_intent" and a["dt"] >= adt
                            and (a["dt"] - adt) <= timedelta(minutes=20)
                        ]
                        retry_event = min(explicit_retry, key=lambda x: x["dt"]) if explicit_retry else None
                        attempt["retry_observation"] = {
                            "app": (retry_event or trigger or {}).get("app"),
                            "search_time": search.get("time") if search else None,
                            "search_delay_seconds": int((search["dt"] - adt).total_seconds()) if search else None,
                            "sent_time": sent.get("time") if sent else None,
                            "sent_delay_seconds": int((sent["dt"] - adt).total_seconds()) if sent else None,
                            "sent_indexer": sent.get("indexer") if sent else None,
                            "explicit_trigger": bool(retry_event),
                            "trigger_time": retry_event.get("time") if retry_event else None,
                            "confidence": "explicit failed-download retry" if retry_event else "observed Arr follow-up",
                        }

                    later = [g for g in matching_grabs if g["dt"] > adt and (g["dt"] - adt) <= timedelta(hours=6)]
                    if later:
                        g = min(later, key=lambda x: x["dt"])
                        attempt["next_grab"] = {
                            "time": g["time"], "indexer": g["indexer"],
                            "match_method": g["match_method"],
                            "delay_seconds": int((g["dt"] - adt).total_seconds()),
                        }

            # Build end-to-end attempt stories.  Unlike the legacy flat timeline,
            # each card keeps the Arr/Prowlarr acquisition context next to the SAB
            # lifecycle it produced, then bridges to the next observed follow-up.
            story_blocks = []
            used_arr_ids = set()
            used_grab_ids = set()

            def fmt_scope(a):
                if not a:
                    return "Search"
                season = a.get("season")
                episode = a.get("episode")
                if season is not None and episode is not None:
                    return f"Episode search · S{int(season):02d}E{int(episode):02d}"
                if season is not None:
                    return f"Season search · S{int(season):02d}"
                return "Search"

            def infer_search_origin(search, prow_ctx=None):
                """Return (mode, confidence) for a drawer search session.

                Normal Arr Info logs often do not literally say "interactive".
                A retained automatic-search completion is strong evidence for
                automatic origin; explicit RSS context is strong evidence for RSS.
                Otherwise the origin remains unknown. Normal Arr Info logs do not
                reliably distinguish a user-clicked automatic search from interactive
                browsing, so guessing would be misleading.
                """
                if search:
                    mode = str(search.get("search_mode") or "unknown").lower()
                    confidence = str(search.get("origin_confidence") or "none").lower()
                    if mode not in {"", "unknown"}:
                        return mode, confidence if confidence != "none" else "high"
                if prow_ctx and prow_ctx.get("kind") == "rss":
                    return "rss", "high"
                return "unknown", "none"

            for idx, attempt in enumerate(attempts):
                final_dt = self._local_dt(attempt["time"])
                handoff_dt = self._local_dt((attempt.get("arr_handoff") or {}).get("time"))
                anchor_dt = handoff_dt or final_dt

                sent = None
                if anchor_dt is not None:
                    candidates = [a for a in matching_arr if a["event_type"] == "sent_to_sab" and a["dt"] <= (final_dt or anchor_dt)]
                    if attempt.get("arr_handoff"):
                        app = attempt["arr_handoff"].get("app")
                        idxname = attempt["arr_handoff"].get("indexer")
                        candidates = [a for a in candidates if (not app or a.get("app") == app) and (not idxname or not a.get("indexer") or a.get("indexer") == idxname)] or candidates
                    if candidates:
                        sent = min(candidates, key=lambda a: abs((a["dt"] - anchor_dt).total_seconds()))
                        if abs((sent["dt"] - anchor_dt).total_seconds()) > 1800:
                            sent = None
                if sent:
                    used_arr_ids.add(sent["event_id"])

                sent_dt = sent["dt"] if sent else handoff_dt
                search = None
                if sent_dt is not None:
                    searches = [a for a in matching_arr if a["event_type"] == "search_started" and a["dt"] <= sent_dt and (sent_dt - a["dt"]) <= timedelta(minutes=45)]
                    if sent and sent.get("app"):
                        same_app = [a for a in searches if a.get("app") == sent.get("app")]
                        searches = same_app or searches
                    if searches:
                        search = max(searches, key=lambda a: a["dt"])
                        used_arr_ids.add(search["event_id"])

                grab = None
                grab_anchor = sent_dt or anchor_dt
                if grab_anchor is not None:
                    gcands = [g for g in matching_grabs if g["dt"] <= grab_anchor and (grab_anchor - g["dt"]) <= timedelta(minutes=30)]
                    if attempt.get("indexer") and attempt["indexer"] != "(unattributed)":
                        same_idx = [g for g in gcands if g.get("indexer") == attempt["indexer"]]
                        gcands = same_idx or gcands
                    if gcands:
                        grab = max(gcands, key=lambda g: g["dt"])
                        used_grab_ids.add(grab["event_id"])

                prow_ctx = None
                if grab:
                    idx_contexts = contexts.get(grab["indexer"], [])
                    # When an explicit Arr search is known, bind Prowlarr context to
                    # the query issued near that search start rather than a later
                    # overlapping RSS poll.  Long Arr decision-making phases can last
                    # minutes after Prowlarr has already returned results.
                    if search:
                        near_search = [
                            ctx for ctx in idx_contexts
                            if search["dt"] - timedelta(seconds=5) <= ctx["dt"] <= search["dt"] + timedelta(minutes=2)
                            and (not ctx.get("source") or str(ctx.get("source") or "").lower() == str(search.get("app") or "").lower())
                        ]
                        if near_search:
                            non_rss = [ctx for ctx in near_search if ctx.get("kind") != "rss"]
                            pool = non_rss or near_search
                            prow_ctx = min(pool, key=lambda ctx: abs((ctx["dt"] - search["dt"]).total_seconds()))
                    if prow_ctx is None:
                        for ctx in idx_contexts:
                            if ctx["dt"] > grab["dt"]:
                                break
                            age = grab["dt"] - ctx["dt"]
                            if timedelta(0) <= age <= timedelta(minutes=15):
                                prow_ctx = ctx

                prev_attempt = attempts[idx - 1] if idx > 0 else None
                prev_dt = self._local_dt(prev_attempt["time"]) if prev_attempt else None
                bridge = None
                if prev_attempt and prev_attempt.get("status") == "failed" and search and prev_dt and search["dt"] > prev_dt:
                    delay = int((search["dt"] - prev_dt).total_seconds())
                    previous_retry = (prev_attempt.get("retry_observation") or {})
                    bridge = {
                        "label": "Automatic retry after previous SAB failure" if previous_retry.get("explicit_trigger") else "Observed follow-up after previous SAB failure",
                        "delay_seconds": delay,
                        "confidence": "causal" if previous_retry.get("explicit_trigger") else "correlation",
                    }

                import_event = None
                if attempt["status"] == "completed" and final_dt is not None:
                    imports = [a for a in matching_arr if a["event_type"] in {"import_succeeded", "import_failed", "import_cancelled"} and a["dt"] >= final_dt and (a["dt"] - final_dt) <= timedelta(hours=2)]
                    if imports:
                        import_event = min(imports, key=lambda a: a["dt"])
                        used_arr_ids.add(import_event["event_id"])

                start_dt = None
                for candidate in (search.get("dt") if search else None, prow_ctx.get("dt") if prow_ctx else None, grab.get("dt") if grab else None, sent_dt):
                    if candidate is not None and (start_dt is None or candidate < start_dt):
                        start_dt = candidate
                duration = int((final_dt - start_dt).total_seconds()) if final_dt and start_dt and final_dt >= start_dt else None

                d = attempt.get("sab_diagnostic") or {}
                h = attempt.get("sab_history") or {}

                # SAB History can retain the per-job ``Servers`` stage.  Turn it
                # into a compact contribution summary for the drawer while keeping
                # the raw stage evidence available for audit.  Values are rounded
                # by SAB itself, so shares are intentionally approximate.
                provider_usage = extract_server_contributions(h.get("stages") or []) if h else {}
                provider_total = sum(int(v or 0) for v in provider_usage.values())
                provider_contributions = [
                    {
                        "provider": server,
                        "provider_label": provider_label(server),
                        "bytes": int(used_bytes),
                        "bytes_human": human_bytes(int(used_bytes)),
                        "share": pct(int(used_bytes), provider_total, 1) or 0.0,
                    }
                    for server, used_bytes in sorted(
                        provider_usage.items(), key=lambda item: int(item[1] or 0), reverse=True
                    )
                    if int(used_bytes or 0) > 0
                ]

                stage = d.get("failure_stage") or h.get("failure_stage")
                cause = d.get("failure_reason") or h.get("fail_message")
                cause_conf = d.get("failure_confidence") or ("medium" if cause else None)
                if attempt["status"] == "failed" and not stage:
                    stage = "Cause unavailable"
                summary = "Completed"
                if attempt["status"] == "failed":
                    summary = f"Failed · {stage or 'Cause unavailable'}"
                elif attempt["status"] == "cancelled":
                    summary = "Cancelled / aborted"
                    stage = "Cancelled"
                    cause = cause or "SAB/Arr recorded the attempt as cancelled or aborted."
                    cause_conf = cause_conf or "high"
                elif int(attempt.get("repair_blocks") or 0) > 0:
                    summary = f"Completed · {int(attempt['repair_blocks'])} repair blocks"

                if duration is None:
                    duration_text = None
                elif duration >= 3600:
                    duration_text = f"{duration // 3600}h {(duration % 3600) // 60}m"
                elif duration >= 60:
                    duration_text = f"{duration // 60}m {duration % 60}s"
                else:
                    duration_text = f"{duration}s"

                search_mode, search_origin_confidence = infer_search_origin(search, prow_ctx)
                sab_evidence = str(d.get("source") or ("history" if h else "inferred"))
                import_state = None
                if attempt["status"] == "completed":
                    arr_app = str((sent or search or {}).get("app") or "").lower()
                    if import_event:
                        import_state = {
                            "status": "confirmed" if import_event.get("event_type") == "import_succeeded" else "failed",
                            "app": str(import_event.get("app") or arr_app or "arr"),
                        }
                    elif arr_app in {"sonarr", "radarr"}:
                        age_seconds = None
                        if final_dt is not None:
                            try:
                                age_seconds = max(0, int((local_now() - final_dt).total_seconds()))
                            except Exception:
                                age_seconds = None
                        import_state = {
                            "status": "pending" if age_seconds is not None and age_seconds < 7200 else "not_observed",
                            "app": arr_app,
                            "age_seconds": age_seconds,
                        }
                story_blocks.append({
                    "kind": "sab_attempt",
                    "attempt_number": idx + 1,
                    "status": attempt["status"],
                    "indexer": attempt.get("indexer"),
                    "summary": summary,
                    "start_time": (search or prow_ctx or grab or sent or {}).get("time") if (search or prow_ctx or grab or sent) else None,
                    "final_time": attempt.get("time"),
                    "duration_seconds": duration,
                    "duration_text": duration_text,
                    "search": {k: v for k, v in search.items() if k != "dt"} if search else None,
                    "search_scope": fmt_scope(search),
                    "search_mode": search_mode,
                    "search_origin_confidence": search_origin_confidence,
                    "prowlarr_context": {k: v for k, v in prow_ctx.items() if k != "dt"} if prow_ctx else None,
                    "grab": {k: v for k, v in grab.items() if k != "dt"} if grab else None,
                    "sent": {k: v for k, v in sent.items() if k != "dt"} if sent else attempt.get("arr_handoff"),
                    "bridge": bridge,
                    "follow_up": attempt.get("retry_observation"),
                    "sab_diagnostic": d or None,
                    "sab_history": h or None,
                    "sab_evidence": sab_evidence,
                    "provider_contributions": provider_contributions,
                    "repair_blocks": int(attempt.get("repair_blocks") or 0),
                    "failure_stage": stage,
                    "failure_reason": cause,
                    "failure_confidence": cause_conf,
                    "import_event": {k: v for k, v in import_event.items() if k != "dt"} if import_event else None,
                    "import_state": import_state,
                    "open_default": False,
                })

            # Retain pre-SAB acquisition failures as first-class story cards.
            # Suppress a low-level grab_error when a higher-level grab_failed
            # record for the same release follows immediately afterward.
            arr_grab_failed = [a for a in matching_arr if a["event_type"] == "grab_failed"]
            for a in matching_arr:
                if a["event_type"] not in {"grab_failed", "grab_error"}:
                    continue
                if a["event_id"] in used_arr_ids:
                    continue
                if a["event_type"] == "grab_error" and any(
                    f.get("release_name") and a.get("release_name")
                    and normalize_match(f["release_name"]) == normalize_match(a["release_name"])
                    and 0 <= (f["dt"] - a["dt"]).total_seconds() <= 90
                    for f in arr_grab_failed
                ):
                    continue
                nearest_search = None
                searches = [x for x in matching_arr if x["event_type"] == "search_started" and x["dt"] <= a["dt"] and (a["dt"] - x["dt"]) <= timedelta(minutes=45)]
                if searches:
                    nearest_search = max(searches, key=lambda x: x["dt"])
                story_blocks.append({
                    "kind": "pre_sab",
                    "attempt_number": None,
                    "status": "failed",
                    "indexer": a.get("indexer"),
                    "summary": "Failed before SAB",
                    "start_time": nearest_search.get("time") if nearest_search else a.get("time"),
                    "final_time": a.get("time"),
                    "duration_seconds": int((a["dt"] - nearest_search["dt"]).total_seconds()) if nearest_search else None,
                    "search": {k: v for k, v in nearest_search.items() if k != "dt"} if nearest_search else None,
                    "search_scope": fmt_scope(nearest_search),
                    "search_mode": infer_search_origin(nearest_search)[0],
                    "search_origin_confidence": infer_search_origin(nearest_search)[1],
                    "prowlarr_context": None,
                    "grab": None,
                    "sent": None,
                    "bridge": None,
                    "follow_up": None,
                    "sab_diagnostic": None,
                    "sab_history": None,
                    "repair_blocks": 0,
                    "failure_stage": "Pre-SAB",
                    "failure_reason": a.get("reason") or a.get("message") or "Release acquisition failed before SABnzbd was reached.",
                    "failure_confidence": "high",
                    "import_event": None,
                    "open_default": True,
                })


            # Search-only and unmatched hand-offs are first-class activity states.
            # They are useful for interactive browsing/power-loss review but never
            # enter indexer success/failure denominators.
            used_sent_times = {
                str((b.get("sent") or {}).get("time") or "")
                for b in story_blocks if b.get("sent")
            }
            used_sent_events = [
                b.get("sent") for b in story_blocks if b.get("sent")
            ]
            sab_story_blocks = [b for b in story_blocks if b.get("kind") == "sab_attempt"]

            def handoff_already_explained(a):
                """Return True when an unmatched hand-off is duplicate evidence for a known attempt.

                Arr can emit more than one hand-off-looking line for a single acquisition,
                especially around season searches.  Do not turn those duplicate orchestration
                records into fake unresolved cards when a same-indexer SAB terminal attempt
                follows shortly afterward.
                """
                aidx = str(a.get("indexer") or "").strip().lower()
                for used in used_sent_events:
                    if not used:
                        continue
                    uidx = str(used.get("indexer") or "").strip().lower()
                    if aidx and uidx and aidx != uidx:
                        continue
                    udt = self._local_dt(used.get("time"))
                    if udt is None or abs((a["dt"] - udt).total_seconds()) > 15 * 60:
                        continue
                    ar = str(a.get("release_name") or "")
                    ur = str(used.get("release_name") or "")
                    same_release = bool(ar and ur and normalize_match(ar) == normalize_match(ur))
                    same_episode = bool(
                        ar and ur and episode_identity(ar) is not None
                        and episode_identity(ar) == episode_identity(ur)
                    )
                    if same_release or same_episode:
                        return True

                for block in sab_story_blocks:
                    bidx = str(block.get("indexer") or "").strip().lower()
                    if aidx and bidx and aidx != bidx:
                        continue
                    fdt = self._local_dt(block.get("final_time"))
                    if fdt is None or fdt < a["dt"] - timedelta(minutes=2):
                        continue
                    if fdt - a["dt"] > timedelta(minutes=20):
                        continue
                    release_name = str(a.get("release_name") or "")
                    compatible = False
                    if release_name:
                        compatible = normalize_match(release_name) == norm
                        if not compatible and ep is not None:
                            compatible = episode_identity(release_name) == ep
                    if not compatible:
                        compatible = arr_media_matches_release(
                            a.get("app"), a.get("media_title"), a.get("season"), a.get("episode"), title
                        )
                    if compatible:
                        return True
                return False

            for a in matching_arr:
                if a["event_type"] != "sent_to_sab" or str(a.get("time") or "") in used_sent_times:
                    continue
                if handoff_already_explained(a):
                    continue
                prior_searches = [x for x in matching_arr if x["event_type"] == "search_started" and x["app"] == a["app"] and x["dt"] <= a["dt"] and (a["dt"] - x["dt"]) <= timedelta(minutes=30)]
                search = max(prior_searches, key=lambda x: x["dt"]) if prior_searches else None
                story_blocks.append({
                    "kind": "unresolved", "attempt_number": None, "status": "unresolved",
                    "indexer": a.get("indexer"), "summary": "Sent to SAB · terminal result not observed",
                    "start_time": (search or a).get("time"), "final_time": a.get("time"),
                    "duration_seconds": None, "duration_text": None,
                    "search": {k: v for k, v in search.items() if k != "dt"} if search else None,
                    "search_scope": fmt_scope(search),
                    "search_mode": infer_search_origin(search)[0],
                    "search_origin_confidence": infer_search_origin(search)[1],
                    "prowlarr_context": None, "grab": None,
                    "sent": {k: v for k, v in a.items() if k != "dt"},
                    "bridge": None, "follow_up": None, "sab_diagnostic": None,
                    "sab_history": None, "repair_blocks": 0, "failure_stage": None,
                    "failure_reason": None, "failure_confidence": None,
                    "import_event": None, "open_default": True,
                })

            if not story_blocks:
                searches = [a for a in matching_arr if a["event_type"] == "search_started"]
                for search in searches:
                    mode, mode_confidence = infer_search_origin(search)
                    story_blocks.append({
                        "kind": "search_only", "attempt_number": None, "status": "search-only",
                        "indexer": None, "summary": "Search only · no release sent to SAB",
                        "start_time": search.get("time"), "final_time": search.get("time"),
                        "duration_seconds": None, "duration_text": None,
                        "search": {k: v for k, v in search.items() if k != "dt"},
                        "search_scope": fmt_scope(search), "search_mode": mode,
                        "search_origin_confidence": mode_confidence,
                        "prowlarr_context": None, "grab": None, "sent": None,
                        "bridge": None, "follow_up": None, "sab_diagnostic": None,
                        "sab_history": None, "repair_blocks": 0, "failure_stage": None,
                        "failure_reason": None, "failure_confidence": None,
                        "import_event": None, "open_default": True,
                    })

            story_blocks.sort(key=lambda b: str(b.get("start_time") or b.get("final_time") or ""))
            sab_story = [b for b in story_blocks if b["kind"] == "sab_attempt"]
            for i, block in enumerate(sab_story):
                is_final = i == len(sab_story) - 1
                specific_failure = block["status"] == "failed" and str(block.get("failure_stage") or "") not in {"", "Unknown", "Cause unavailable"}
                block["open_default"] = bool(
                    len(sab_story) == 1 or is_final or specific_failure
                )

            timeline = []

            # Arr orchestration events.  queued_to_sab is suppressed when a
            # stronger sent_to_sab record exists within a few seconds.  Likewise
            # a low-level grab_error is suppressed when the higher-level
            # grab_failed line records the same release immediately afterward.
            sent_events = [a for a in matching_arr if a["event_type"] == "sent_to_sab"]
            failed_events = [a for a in matching_arr if a["event_type"] == "grab_failed"]
            for a in matching_arr:
                et = a["event_type"]
                if et == "queued_to_sab":
                    if any(
                        s.get("release_name") and a.get("release_name")
                        and normalize_match(s["release_name"]) == normalize_match(a["release_name"])
                        and abs((s["dt"] - a["dt"]).total_seconds()) <= 8
                        for s in sent_events
                    ):
                        continue
                if et == "grab_error":
                    if any(
                        f.get("release_name") and a.get("release_name")
                        and normalize_match(f["release_name"]) == normalize_match(a["release_name"])
                        and 0 <= (f["dt"] - a["dt"]).total_seconds() <= 90
                        for f in failed_events
                    ):
                        continue

                app_label = a["app"].title()
                if et == "search_started":
                    media = a.get("media_title") or "matching media"
                    if a.get("season") is not None:
                        media += f" · S{int(a['season']):02d}"
                    if a.get("episode") is not None:
                        media += f"E{int(a['episode']):02d}"
                    active = a.get("details", {}).get("active_indexers")
                    detail = media + (f" · {active} active indexers" if active is not None else "")
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "search",
                        "indexer": None, "title": f"{app_label} search", "detail": detail,
                        "source_app": a["app"],
                    })
                elif et == "retry_search_intent":
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "search",
                        "indexer": None, "title": f"{app_label} automatic retry triggered",
                        "detail": "Failed-download handling explicitly requested another search",
                        "source_app": a["app"],
                    })
                elif et == "sent_to_sab":
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "grab",
                        "indexer": a.get("indexer"), "title": f"{app_label} sent to SAB",
                        "detail": f"{a['match_method']} release hand-off",
                        "source_app": a["app"],
                    })
                elif et == "queued_to_sab":
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "grab",
                        "indexer": None, "title": f"{app_label} queued in SAB",
                        "detail": "download-client queue hand-off", "source_app": a["app"],
                    })
                elif et in {"grab_failed", "grab_error"}:
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "failed",
                        "indexer": a.get("indexer"), "title": f"{app_label} grab failed",
                        "detail": a.get("reason") or a.get("message") or "pre-SAB grab failure",
                        "source_app": a["app"],
                    })
                elif et == "import_succeeded":
                    detail = "media imported/assigned"
                    if a.get("details", {}).get("file"):
                        detail += f" · {a['details']['file']}"
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "completed",
                        "indexer": None, "title": f"{app_label} import completed",
                        "detail": detail, "source_app": a["app"],
                    })
                elif et == "import_failed":
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "failed",
                        "indexer": None, "title": f"{app_label} import failed",
                        "detail": a.get("reason") or a.get("message") or "import failure",
                        "source_app": a["app"],
                    })
                elif et == "history_removed":
                    removal_reason = str((a.get("details") or {}).get("removal_reason") or a.get("reason") or "")
                    detail = {
                        "arr_remove_completed_history": "Completed item removed from SAB history after import",
                        "arr_remove_failed_history": "Failed item removed from SAB history during failed-download handling",
                    }.get(removal_reason, "Item removed from SAB history")
                    timeline.append({
                        "time": a["time"], "dt": a["dt"], "kind": "history",
                        "indexer": None, "title": f"{app_label} SAB history cleanup",
                        "detail": detail, "source_app": a["app"],
                    })

            used_context_ids = set()
            context_window = timedelta(minutes=15)
            for grab in matching_grabs:
                prior = None
                for ctx in contexts.get(grab["indexer"], []):
                    if ctx["dt"] > grab["dt"]:
                        break
                    age = grab["dt"] - ctx["dt"]
                    if timedelta(0) <= age <= context_window:
                        prior = ctx
                if prior and prior["event_id"] not in used_context_ids:
                    used_context_ids.add(prior["event_id"])
                    detail_parts = []
                    if prior["query_type"]:
                        detail_parts.append(prior["query_type"])
                    if prior["query"]:
                        detail_parts.append(f"query: {prior['query']}")
                    if prior["results"] not in (None, ""):
                        detail_parts.append(f"{prior['results']} results")
                    if prior["source"]:
                        detail_parts.append(f"source: {prior['source']}")
                    timeline.append({
                        "time": prior["time"], "dt": prior["dt"], "kind": prior["kind"],
                        "indexer": prior["indexer"],
                        "title": "RSS check" if prior["kind"] == "rss" else "Indexer search",
                        "detail": " · ".join(detail_parts) or "Prowlarr history context",
                    })

                grab_details = []
                if grab["grab_method"]:
                    grab_details.append(grab["grab_method"])
                if grab["source"]:
                    grab_details.append(f"source: {grab['source']}")
                if grab["match_method"]:
                    grab_details.append(f"{grab['match_method']} title")
                timeline.append({
                    "time": grab["time"], "dt": grab["dt"], "kind": "grab",
                    "indexer": grab["indexer"], "title": "Release grabbed",
                    "detail": " · ".join(grab_details) or grab["title"],
                })

            for item in attempts:
                dt = self._local_dt(item["time"])
                if dt is None:
                    continue
                detail = f"{item['match_method']} attribution · {item['repair_blocks']} repair blocks"
                d = item.get("sab_diagnostic") or {}
                h = item.get("sab_history") or {}
                if item["status"] == "failed" and d.get("failure_reason"):
                    stage = d.get("failure_stage") or "failure"
                    detail += f" · {stage}: {d['failure_reason']}"
                elif item["status"] == "failed" and h.get("fail_message"):
                    stage = h.get("failure_stage") or "failure"
                    detail += f" · {stage}: {h['fail_message']}"
                timeline.append({
                    "time": item["time"], "dt": dt, "kind": item["status"],
                    "indexer": item["indexer"], "title": f"SAB {item['status']}",
                    "detail": detail,
                })

            timeline.sort(key=lambda x: x["dt"])
            for item in timeline:
                item.pop("dt", None)

            return {
                "attempts": attempts,
                "grabs": [{k: v for k, v in g.items() if k != "dt"} for g in matching_grabs],
                "arr_events": [{k: v for k, v in a.items() if k != "dt"} for a in matching_arr],
                "timeline": timeline,
                "story_blocks": story_blocks,
                "has_sab_history": bool(sab_hist),
                "has_sab_diagnostics": any(bool(x.get("sab_diagnostic")) for x in attempts),
                "has_arr_events": bool(matching_arr),
                "arr_apps": sorted({a["app"] for a in matching_arr}),
                "correlation_confidence": (
                    "exact Arr hand-off"
                    if any(a["event_type"] == "sent_to_sab" and a["match_method"] == "exact" for a in matching_arr)
                    else "media/time correlation" if matching_arr else "best-effort correlation"
                ),
                "correlation_note": (
                    "Sonarr/Radarr events come from their rotating logs. Exact release hand-offs to SAB are "
                    "treated as strong correlation; media/search context and Prowlarr search/RSS context remain "
                    "best-effort timing correlations. SAB lifecycle/failure evidence comes from rotating SAB logs "
                    "and is enriched by SABnzbd History API data when available."
                ),
            }

