"""Run Harbor jobs and read their results.

Harbor is driven through its CLI, exactly as the README shows, so what the
pipeline measures is what a reviewer gets by running the same command.
Credentials are passed from .env; provider variables in the calling shell are
dropped first.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from generator import config
from pipeline.store import to_record_path

STDOUT_TAIL_CHARS = 14000


class _JobLimiter:
    """Caps how many Harbor jobs this process runs at once and records the peak."""

    def __init__(self, limit: int):
        self._lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(limit)
        self.limit = limit
        self.running = 0
        self.peak = 0
        self.total = 0

    def set_limit(self, limit: int) -> None:
        """Change the cap. Call before any job starts."""
        with self._lock:
            if self.running:
                raise RuntimeError("cannot change the Harbor job limit while jobs are running")
            self.limit = limit
            self._semaphore = threading.BoundedSemaphore(limit)

    def __enter__(self):
        self._semaphore.acquire()
        with self._lock:
            self.running += 1
            self.total += 1
            self.peak = max(self.peak, self.running)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self.running -= 1
        self._semaphore.release()
        return False


job_limiter = _JobLimiter(config.MAX_HARBOR_JOBS)


@dataclass
class TrialResult:
    trial_name: str
    trial_dir: str
    reward: float | None
    exception_type: str | None
    exception_message: str | None
    cost_usd: float
    n_input_tokens: int
    n_output_tokens: int
    n_episodes: int | None
    duration_sec: float | None
    tests: list[dict] = field(default_factory=list)
    has_test_report: bool = False
    test_stdout_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.reward == 1.0

    @property
    def failed_tests(self) -> list[dict]:
        return [t for t in self.tests if t["status"] != "passed"]

    def to_dict(self) -> dict:
        return asdict(self)


def harbor_binary() -> str:
    """Prefer the harbor installed next to the running interpreter."""
    beside = Path(sys.executable).parent / "harbor"
    if beside.exists():
        return str(beside)
    found = shutil.which("harbor") or str(Path.home() / ".local/bin/harbor")
    if not Path(found).exists():
        raise RuntimeError("harbor is not installed (see README: `uv tool install harbor`).")
    return found


def _harbor_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENROUTER_", "OPENAI_", "ANTHROPIC_"))
    }
    key, base = config.gateway_credentials()
    env["OPENROUTER_API_KEY"] = key
    env["OPENROUTER_API_BASE"] = base
    return env


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    from datetime import datetime

    try:
        fmt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        return round((fmt(end) - fmt(start)).total_seconds(), 1)
    except ValueError:
        return None


def read_trial(trial_dir: Path) -> TrialResult | None:
    """Parse one trial directory. Returns None if the trial never finished."""
    result_path = trial_dir / "result.json"
    if not result_path.exists():
        return None
    data = json.loads(result_path.read_text())
    agent = data.get("agent_result") or {}
    verifier = data.get("verifier_result") or {}
    exception = data.get("exception_info") or {}

    tests: list[dict] = []
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    has_report = False
    if ctrf_path.exists():
        try:
            ctrf = json.loads(ctrf_path.read_text())
            for test in ctrf["results"]["tests"]:
                tests.append(
                    {
                        "name": test.get("name", "").split("::", 1)[-1],
                        "status": test.get("status"),
                        "message": (test.get("message") or "")[:1500],
                    }
                )
            has_report = True
        except (json.JSONDecodeError, KeyError, TypeError):
            has_report = False

    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    stdout_tail = ""
    if stdout_path.exists():
        stdout_tail = stdout_path.read_text(errors="replace")[-STDOUT_TAIL_CHARS:]

    reward = (verifier.get("rewards") or {}).get("reward")
    return TrialResult(
        trial_name=data.get("trial_name", trial_dir.name),
        trial_dir=to_record_path(trial_dir),
        reward=float(reward) if reward is not None else None,
        exception_type=exception.get("exception_type"),
        exception_message=(exception.get("exception_message") or "")[:3000] or None,
        cost_usd=float(agent.get("cost_usd") or 0.0),
        n_input_tokens=int(agent.get("n_input_tokens") or 0),
        n_output_tokens=int(agent.get("n_output_tokens") or 0),
        n_episodes=(agent.get("metadata") or {}).get("n_episodes"),
        duration_sec=_seconds_between(data.get("started_at"), data.get("finished_at")),
        tests=tests,
        has_test_report=has_report,
        test_stdout_tail=stdout_tail,
    )


def read_job(job_dir: Path) -> list[TrialResult]:
    if not job_dir.is_dir():
        return []
    trials = []
    for child in sorted(job_dir.iterdir()):
        if child.is_dir():
            trial = read_trial(child)
            if trial is not None:
                trials.append(trial)
    return trials


def run_job(
    task_dir: Path,
    agent: str,
    jobs_dir: Path,
    job_name: str,
    *,
    n_attempts: int = 1,
    model: str | None = None,
    agent_kwargs: dict[str, str] | None = None,
    timeout_sec: float | None = None,
) -> tuple[list[TrialResult], str]:
    """Run one Harbor job and return (finished trials, tail of harbor's output).

    A job directory left over from a killed run is removed first: its trials
    are incomplete and Harbor would otherwise refuse or mix results.

    Waits for a free slot first: at most ``config.MAX_HARBOR_JOBS`` jobs run at
    once in this process, so adding workers does not multiply Docker builds.
    """
    with job_limiter:
        return _run_job(task_dir, agent, jobs_dir, job_name, n_attempts, model, agent_kwargs, timeout_sec)


def _run_job(task_dir, agent, jobs_dir, job_name, n_attempts, model, agent_kwargs, timeout_sec):
    job_dir = jobs_dir / job_name
    if job_dir.exists():
        shutil.rmtree(job_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    command = [
        harbor_binary(), "run",
        "-p", str(task_dir),
        "-a", agent,
        "-o", str(jobs_dir),
        "--job-name", job_name,
        "-k", str(n_attempts),
        "-n", str(n_attempts),
        "-y", "-q",
    ]  # fmt: skip
    if model:
        command += ["-m", model]
    for key, value in (agent_kwargs or {}).items():
        command += ["--ak", f"{key}={value}"]

    started = time.time()
    try:
        proc = subprocess.run(
            command,
            env=_harbor_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(config.ROOT_DIR),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        output = f"harbor timed out after {time.time() - started:.0f}s\n{exc.stdout or ''}"
    return read_job(job_dir), output[-4000:]
