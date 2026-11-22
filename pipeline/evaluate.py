"""Band evaluation: how often does the reference agent solve the task?

Five valid attempts with terminus-2 on GLM-5.3-Flash at high reasoning effort.
The task is learnable if 1 to 3 of them pass.

Each attempt is its own Harbor job and its result is stored as soon as it
finishes, so a killed run keeps (and never pays again for) finished attempts.
"""

import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generator import config
from pipeline.harbor_runner import TrialResult, run_job
from pipeline.store import RunStore, atomic_write_json, read_json

MAX_ATTEMPTS = 9  # 5 valid runs plus room to replace invalid ones
ATTEMPT_TIMEOUT_SEC = config.AGENT_TIMEOUT_SEC + 1800  # agent limit + build and grading


def classify_attempt(trial: TrialResult | None) -> tuple[str, str]:
    """Return (outcome, why) where outcome is 'pass', 'fail' or 'invalid'.

    An attempt is invalid when it says nothing about whether the model can
    solve the task: Harbor produced no result, the tests never ran, the agent
    hit its time limit without passing, or the run was cut short by an error
    (for example a gateway failure). Invalid attempts are replaced, not counted.
    A run that passes is a pass even if it used all of its time.
    """
    if trial is None:
        return "invalid", "harbor produced no result"
    if trial.reward is None:
        return "invalid", f"no reward ({trial.exception_type or 'unknown error'})"
    if not trial.has_test_report:
        return "invalid", "the tests did not run (no test report)"
    if trial.reward == 1.0:
        return "pass", "all tests passed"
    if trial.exception_type and "Timeout" in trial.exception_type:
        return "invalid", f"agent timed out ({trial.exception_type})"
    if trial.exception_type:
        return "invalid", f"run ended in an error ({trial.exception_type})"
    return "fail", f"{len(trial.failed_tests)} test(s) failed"


def band_verdict(passes: int) -> str:
    if passes < config.BAND_LOW:
        return "too_hard"
    if passes > config.BAND_HIGH:
        return "too_easy"
    return "learnable"


def _run_attempt(store: RunStore, task_id: str, task_path: Path, band_dir: Path, jobs_dir: Path, tag: str, number: int) -> dict:
    """Run attempt ``number`` unless its result is already stored."""
    path = band_dir / f"a{number}.json"
    cached = read_json(path)
    if cached:
        return cached

    trials, output = run_job(
        task_path,
        config.EVAL_AGENT,
        jobs_dir,
        f"{tag}-a{number}",
        model=config.EVAL_MODEL,
        agent_kwargs={"reasoning_effort": config.EVAL_REASONING_EFFORT},
        timeout_sec=ATTEMPT_TIMEOUT_SEC,
    )
    trial = trials[0] if trials else None
    outcome, why = classify_attempt(trial)
    record = {
        "attempt": number,
        "outcome": outcome,
        "why": why,
        "trial": trial.to_dict() if trial else None,
        "harbor_output_tail": "" if trial else output,
    }
    atomic_write_json(path, record)
    store.log_event(
        task_id,
        "band_attempt",
        outcome,
        attempt=number,
        why=why,
        cost_usd=trial.cost_usd if trial else 0.0,
        duration_sec=trial.duration_sec if trial else None,
        episodes=trial.n_episodes if trial else None,
        evidence=str(path.relative_to(store.run_dir)),
    )
    return record


def evaluate_band(store: RunStore, task_id: str, task_path: Path, version: int, label: str = "band") -> dict:
    """Measure the pass count over the first N_RUNS valid attempts.

    ``label`` separates independent measurements of the same version. Only
    "band", the verdict measurement, is taken now; some early runs also have
    a second one stored under "confirm".
    """
    tdir = store.task_dir(task_id)
    band_dir = tdir / label / f"v{version}"
    summary_path = band_dir / "summary.json"
    cached = read_json(summary_path)
    if cached:
        return cached

    tag = f"{store.run_dir.name}-{task_id}-v{version}-{label}"
    jobs_dir = tdir / "jobs"
    records: dict[int, dict] = {}
    next_number = 1
    while True:
        valid = [r for r in records.values() if r["outcome"] != "invalid"]
        missing = config.N_RUNS - len(valid)
        if missing <= 0 or next_number > MAX_ATTEMPTS:
            break
        numbers = list(range(next_number, min(next_number + missing, MAX_ATTEMPTS + 1)))
        next_number = numbers[-1] + 1
        with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
            for record in pool.map(
                lambda n: _run_attempt(store, task_id, task_path, band_dir, jobs_dir, tag, n), numbers
            ):
                records[record["attempt"]] = record

    ordered = [records[n] for n in sorted(records)]
    valid = [r for r in ordered if r["outcome"] != "invalid"][: config.N_RUNS]
    passes = sum(r["outcome"] == "pass" for r in valid)
    complete = len(valid) == config.N_RUNS
    steps = [(r["trial"] or {}).get("n_episodes") for r in valid]
    steps = [s for s in steps if s]
    summary = {
        "version": version,
        "label": label,
        "complete": complete,
        "passes": passes,
        "valid_runs": len(valid),
        "invalid_runs": len(ordered) - len(valid),
        "counted_attempts": [r["attempt"] for r in valid],
        "verdict": band_verdict(passes) if complete else "inconclusive",
        "outcomes": [{"attempt": r["attempt"], "outcome": r["outcome"], "why": r["why"]} for r in ordered],
        "cost_usd": round(sum((r["trial"] or {}).get("cost_usd", 0.0) for r in ordered), 6),
        "agent_seconds": round(sum((r["trial"] or {}).get("duration_sec") or 0 for r in ordered), 1),
        # How long the agent worked, in its own steps. Short runs mean the task was read off, not worked out.
        "median_steps": statistics.median(steps) if steps else None,
    }
    atomic_write_json(summary_path, summary)
    store.log_event(
        task_id,
        label,
        summary["verdict"],
        version=version,
        passes=passes,
        valid_runs=len(valid),
        invalid_runs=summary["invalid_runs"],
        cost_usd=summary["cost_usd"],
        evidence=str(summary_path.relative_to(store.run_dir)),
    )
    return summary
