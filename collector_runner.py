from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from config import SECRET_ENV, collector_command, load_config
from runtime import local_now, redact


def now_iso() -> str:
    return local_now().isoformat(timespec="seconds")


class CollectorRunner:
    def __init__(self, config_path: Path, db_path: Path):
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._started_perf: float | None = None
        self._state: dict[str, Any] = {
            "running": False, "status": "idle", "started_at": None,
            "finished_at": None, "returncode": None, "message": "", "output_tail": "",
            "duration_seconds": None, "before_counts": {}, "after_counts": {},
            "delta_attempts": 0, "delta_grab_events": 0,
            "delta_history_rows": 0, "delta_arr_events": 0,
        }

    def _load_config(self) -> dict:
        return load_config(self.config_path, self.db_path)

    def configured(self) -> bool:
        try:
            self._load_config()
            return True
        except Exception:
            return False

    def config_message(self) -> str:
        try:
            cfg = self._load_config()
            return f"Ready: {Path(cfg['script']).name}"
        except Exception as exc:
            return str(exc)

    def _db_counts(self) -> dict[str, int]:
        counts = {"attempts": 0, "history_rows": 0, "grab_events": 0, "arr_events": 0}
        if not self.db_path.is_file():
            return counts
        try:
            conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                for key, query in (
                    ("attempts", "SELECT COUNT(*) FROM jobs"),
                    ("history_rows", "SELECT COUNT(*) FROM prowlarr_history"),
                    ("grab_events", "SELECT COUNT(*) FROM prowlarr_history WHERE lower(event_type) LIKE '%grab%'"),
                    ("arr_events", "SELECT COUNT(*) FROM arr_events"),
                ):
                    try:
                        counts[key] = int(conn.execute(query).fetchone()[0])
                    except sqlite3.Error:
                        pass
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        return counts

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            started = self._started_perf
        if state["running"] and started is not None:
            state["duration_seconds"] = round(time.perf_counter() - started, 1)
        state["configured"] = self.configured()
        state["config_message"] = self.config_message()
        return state

    def start(self) -> dict[str, Any]:
        cfg = self._load_config()
        # Reserve the run atomically before any I/O or thread startup.
        with self._lock:
            already_running = self._state["running"]
            if not already_running:
                self._started_perf = time.perf_counter()
                self._state.update(
                    running=True, status="running", started_at=now_iso(), finished_at=None,
                    returncode=None, message="Collector is running…", output_tail="",
                    duration_seconds=0.0, before_counts={}, after_counts={}, delta_attempts=0,
                    delta_grab_events=0, delta_history_rows=0, delta_arr_events=0,
                )
        if already_running:
            return self.status()
        try:
            before = self._db_counts()
            with self._lock:
                self._state["before_counts"] = before
            threading.Thread(target=self._worker, args=(cfg,), name="nzbview-collector", daemon=True).start()
        except Exception as exc:
            self._finish("error", f"Collector failed to start: {exc}")
            raise
        return self.status()

    def _finish(self, status: str, message: str, output: str = "", returncode=None, after=None):
        with self._lock:
            before = self._state.get("before_counts") or {}
            after = after or {}
            duration = round(time.perf_counter() - (self._started_perf or time.perf_counter()), 1)
            self._state.update(
                running=False, status=status, message=message, output_tail=output[-7000:],
                returncode=returncode, finished_at=now_iso(), duration_seconds=duration,
                after_counts=after,
            )
            for label, key in (("attempts", "attempts"), ("grab_events", "grab_events"),
                               ("history_rows", "history_rows"), ("arr_events", "arr_events")):
                self._state[f"delta_{label}"] = max(0, after.get(key, 0) - before.get(key, 0))
            self._started_perf = None

    def _worker(self, cfg: dict) -> None:
        secrets = [cfg.get(key, "") for key in SECRET_ENV]
        try:
            cmd, env = collector_command(cfg)
            result = subprocess.run(
                cmd, cwd=str(Path(cfg["script"]).parent), env=env,
                text=True, encoding="utf-8", errors="replace", capture_output=True,
                timeout=cfg["timeout_seconds"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = redact((result.stdout + "\n" + result.stderr).strip(), secrets)
            ok = result.returncode == 0
            self._finish(
                "success" if ok else "partial" if result.returncode == 3 else "error",
                "Collector completed successfully." if ok else "Logs imported; some API sources were unavailable. See Diagnostics." if result.returncode == 3 else f"Collector exited with code {result.returncode}. See collector output for details.",
                output, result.returncode, self._db_counts(),
            )
        except subprocess.TimeoutExpired as exc:
            def decode(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
            self._finish("error", "Collector timed out.", redact(decode(exc.stdout) + "\n" + decode(exc.stderr), secrets))
        except Exception as exc:
            self._finish("error", redact(f"Collector failed: {exc}", secrets))
