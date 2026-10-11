"""Small standard-library helpers shared by collection and analytics."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def log_timezone():
    name = os.environ.get("NZBVIEW_LOG_TIMEZONE", "").strip()
    return ZoneInfo(name) if name else None


def local_now() -> datetime:
    zone = log_timezone()
    return datetime.now(zone) if zone else datetime.now().astimezone()


def localize(dt: datetime) -> datetime:
    zone = log_timezone()
    if dt.tzinfo is None and zone:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(zone) if zone else dt.astimezone()


def redact(text: str, secrets=()) -> str:
    for value in secrets:
        if value:
            text = text.replace(str(value), "[REDACTED]")
    return re.sub(r"(?i)((?:api[_-]?key|apikey|password|token)=)[^&\s\"'<>]+", r"\1[REDACTED]", text)


@contextmanager
def collection_lock(db_path: Path):
    """OS-backed lock released automatically if a process exits or crashes."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(db_path) + ".collector.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another collector or maintenance command is using this database.") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Another collector or maintenance command is using this database.") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def complete_lines(stream):
    """Leave an incomplete tail unread so the next run sees the whole line."""
    while True:
        offset = stream.tell()
        line = stream.readline()
        if not line:
            return
        if not line.endswith(b"\n"):
            stream.seek(offset)
            return
        yield line
