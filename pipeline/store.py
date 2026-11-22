"""On-disk records for a pipeline run.

Layout (see docs/DESIGN.md):

    runs/<run_id>/
        run.json
        events.jsonl            append-only ledger, one JSON object per line
        tasks/<task_id>/...     everything about one candidate task

Writes are atomic (temp file + rename) so a reader, or a second copy of the
pipeline, never sees a half-written file. Ledger appends take a file lock.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# Records must not depend on where the repository happens to be checked out.
REPO_ROOT = Path(__file__).resolve().parent.parent


def to_record_path(path) -> str:
    """How a path is written into a record: relative to the repository root when it is inside it."""
    path = Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)  # outside the repository (for example a test's temp dir): keep it as it is


def from_record_path(value) -> Path:
    """Read a path back from a record. Old records hold absolute paths; those are used unchanged."""
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    """Exclusive advisory lock on ``path``. Yields False if non-blocking and held.

    The lock belongs to the open file description, so the operating system
    releases it when the process dies, however it dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


class RunStore:
    """A run directory: config, ledger and per-task folders."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.tasks_dir = self.run_dir / "tasks"
        self.ledger_path = self.run_dir / "events.jsonl"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        path = self.tasks_dir / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_event(self, task_id: str | None, stage: str, outcome: str, **fields) -> dict:
        """Append one event to the ledger. Short single write under a lock."""
        event = {
            "ts": now_iso(),
            "pid": os.getpid(),
            "task_id": task_id,
            "stage": stage,
            "outcome": outcome,
            **fields,
        }
        line = json.dumps(event, sort_keys=False) + "\n"
        with file_lock(self.run_dir / ".ledger.lock"):
            with open(self.ledger_path, "a") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return event

    def events(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        events = []
        for line in self.ledger_path.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events


class Timer:
    """Context manager measuring wall-clock seconds."""

    def __enter__(self):
        self._start = time.time()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.time() - self._start, 2)
        return False
