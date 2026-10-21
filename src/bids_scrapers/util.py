from __future__ import annotations

import contextlib
import gzip
import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no locking, just don't start two runs at once
    fcntl = None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_local() -> str:
    return datetime.now().date().isoformat()


def setup_logging(data_dir: Path, verbose: bool = False) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "scrape.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@contextlib.contextmanager
def run_lock(data_dir: Path, name: str):
    """Prevent two runs of the same collector at once (e.g. a slow run overlapping the next timer)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f".{name}.lock"
    with open(path, "w") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(f"Another '{name}' run is in progress ({path})") from None
        fh.write(str(os.getpid()))
        fh.flush()
        yield


class JsonlGzWriter:
    """Appends JSON records to a .jsonl.gz file (each run adds a new gzip member, which is valid)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = gzip.open(path, "at", encoding="utf-8")

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> JsonlGzWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
