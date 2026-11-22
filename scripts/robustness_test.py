"""Part 4, tested for real: kill a running batch and resume it; run two copies at once.

    .venv/bin/python scripts/robustness_test.py kill-resume --run-id rb1 --n 4
    .venv/bin/python scripts/robustness_test.py two-copies  --run-id rb2 --n 6

Both start real batches (model calls, Docker, agent runs) and then check the
records, without trusting the pipeline's own account of what it did.

kill-resume
    Starts a batch, waits until at least two agent runs of the band
    measurement have finished, takes a fingerprint of every record on disk,
    and kills the whole process group with SIGKILL. Then it runs the same
    command again to the end and checks that nothing finished before the kill
    was done or paid for again: every model call, gate result and agent run
    recorded before the kill is still there, byte for byte, and no step that
    had finished was repeated.

two-copies
    Starts the same command twice at the same moment and waits for both. Then
    it checks that every task was worked on by exactly one of them, that no
    step ran twice, that every record parses, and that every line of the
    shared ledger is whole.

Each writes its findings to runs/robustness/<run_id>.json and
exits non-zero if a check failed.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
OUT = ROOT / "runs" / "robustness"
# Steps that happen once per task version or once per task; a second copy of any of these is work done twice.
ONCE_PER_TASK = ("pitch", "spec", "spec_review", "spec_revise", "environment", "tests")


def command(run_id: str, n: int, workers: int) -> list[str]:
    return [PYTHON, "-m", "pipeline.run", "--run-id", run_id, "--n", str(n), "--workers", str(workers), "--harbor-jobs", "20"]


def events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def fingerprint(run_dir: Path) -> dict[str, str]:
    """sha1 of every finished record: model calls, gate results, agent runs, measurements, designs."""
    found = {}
    for pattern in ("tasks/*/llm/*.json", "tasks/*/gates/*.json", "tasks/*/band/*/a*.json", "tasks/*/band/*/summary.json", "tasks/*/spec.json", "tasks/*/seed.json", "tasks/*/reviews/*.json"):
        for path in run_dir.glob(pattern):
            found[str(path.relative_to(run_dir))] = hashlib.sha1(path.read_bytes()).hexdigest()
    return found


def llm_calls(run_dir: Path) -> dict[str, Counter]:
    calls: dict[str, Counter] = {}
    for path in run_dir.glob("tasks/*/llm/*.json"):
        record = json.loads(path.read_text())
        calls.setdefault(path.parts[-3], Counter())[record.get("purpose")] += 1
    return calls


# What the pipeline adds to a prompt when it asks again because the last reply could not be used.
REASK = "Your previous reply could not be used"


def first_asks(run_dir: Path) -> dict[str, Counter]:
    """Per task, how many times each step was begun.

    A step whose reply was cut off or could not be parsed is asked again in
    the same step, with a note saying so; that is one step, not two. A step
    begun twice is work done twice.
    """
    begun: dict[str, Counter] = {}
    for path in run_dir.glob("tasks/*/llm/*.json"):
        record = json.loads(path.read_text())
        if REASK not in json.dumps(record.get("messages") or ""):
            begun.setdefault(path.parts[-3], Counter())[record.get("purpose")] += 1
    return begun


def cost(run_dir: Path) -> float:
    model = sum(json.loads(p.read_text()).get("cost_usd") or 0 for p in run_dir.glob("tasks/*/llm/*.json"))
    agents = sum(e.get("cost_usd") or 0 for e in events(run_dir) if e.get("stage") == "band_attempt")
    return round(model + agents, 4)


def remove_leftover_containers(run_id: str) -> int:
    """A SIGKILL leaves the containers of the runs in flight. Remove them (they hold Docker networks)."""
    listing = subprocess.run(
        ["docker", "ps", "-a", "--format", '{{.ID}} {{.Label "com.docker.compose.project.working_dir"}}'], capture_output=True, text=True
    ).stdout
    ids = [line.split()[0] for line in listing.splitlines() if f"/runs/{run_id}/" in line]
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
    return len(ids)


def records_parse(run_dir: Path) -> list[str]:
    bad = []
    for path in run_dir.rglob("*.json"):
        if "jobs" in path.parts:
            continue
        try:
            json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError) as exc:
            bad.append(f"{path.relative_to(run_dir)}: {exc}")
    for number, line in enumerate((run_dir / "events.jsonl").read_text().splitlines(), 1):
        try:
            json.loads(line)
        except ValueError:
            bad.append(f"events.jsonl line {number} is not whole")
    return bad


def duplicated_steps(run_dir: Path) -> list[str]:
    """A once-only step begun more than once for a task. Asking again after an unusable reply is the same step."""
    problems = []
    for task, begun in sorted(first_asks(run_dir).items()):
        for purpose in ONCE_PER_TASK:
            if begun[purpose] > 1:
                problems.append(f"{task}: {purpose} was done {begun[purpose]} times")
    return problems


def attempts_consistent(run_dir: Path) -> list[str]:
    problems = []
    for summary_path in run_dir.glob("tasks/*/band/*/summary.json"):
        summary = json.loads(summary_path.read_text())
        counted = summary.get("counted_attempts") or []
        if len(counted) != len(set(counted)):
            problems.append(f"{summary_path.parts[-4]}: an attempt is counted twice: {counted}")
        if summary.get("complete") and len(counted) != 5:
            problems.append(f"{summary_path.parts[-4]}: complete with {len(counted)} counted attempts")
        for number in counted:
            if not (summary_path.parent / f"a{number}.json").exists():
                problems.append(f"{summary_path.parts[-4]}: counted attempt {number} has no record")
    return problems


def finals(run_dir: Path) -> dict[str, str]:
    return {p.parent.name: (json.loads(p.read_text()).get("final") or "no verdict") for p in sorted(run_dir.glob("tasks/*/state.json"))}


def kill_resume(run_id: str, n: int, workers: int) -> dict:
    run_dir = ROOT / "runs" / run_id
    log = open(ROOT / "runs" / f"{run_id}.log", "a")
    started = time.time()
    proc = subprocess.Popen(command(run_id, n, workers), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    # Wait for a moment worth killing at: agent runs finished and recorded, others still in flight.
    while proc.poll() is None:
        finished_attempts = [e for e in events(run_dir) if e.get("stage") == "band_attempt" and e.get("outcome") in ("pass", "fail")]
        if len(finished_attempts) >= 2:
            break
        time.sleep(5)
    if proc.poll() is not None:
        return {"test": "kill-resume", "run_id": run_id, "passed": False, "problems": ["the batch finished before any agent run could be interrupted"]}

    before = fingerprint(run_dir)
    events_before = events(run_dir)
    calls_before = llm_calls(run_dir)
    begun_before = first_asks(run_dir)
    cost_before = cost(run_dir)
    stages_at_kill = {p.parent.name: json.loads(p.read_text()).get("status") for p in sorted(run_dir.glob("tasks/*/state.json"))}
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    killed_after = round(time.time() - started)
    time.sleep(5)
    removed = remove_leftover_containers(run_id)

    resumed = subprocess.run(command(run_id, n, workers), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    after = fingerprint(run_dir)
    problems = []
    lost = [path for path in before if path not in after]
    changed = [path for path in before if path in after and after[path] != before[path] and not path.endswith("summary.json")]
    if lost:
        problems.append(f"{len(lost)} record(s) from before the kill are gone: {lost[:5]}")
    if changed:
        problems.append(f"{len(changed)} record(s) from before the kill were rewritten: {changed[:5]}")
    calls_after = llm_calls(run_dir)
    begun_after = first_asks(run_dir)
    for task, begun in begun_before.items():
        for purpose in ONCE_PER_TASK:
            # A call that had returned before the kill and was begun afresh after it was paid for twice.
            if begun[purpose] and begun_after[task][purpose] > begun[purpose]:
                problems.append(f"{task}: {purpose} had been done before the kill and was done again after it")
    attempts_before = sorted(p for p in before if "/band/" in p and Path(p).name.startswith("a"))
    problems += duplicated_steps(run_dir) + attempts_consistent(run_dir) + records_parse(run_dir)
    if resumed.returncode != 0:
        problems.append(f"the resumed batch exited with code {resumed.returncode}")
    starts = [e for e in events(run_dir) if e.get("stage") == "run" and e.get("outcome") == "start"]
    return {
        "test": "kill-resume", "run_id": run_id, "passed": not problems, "problems": problems,
        "killed_after_seconds": killed_after, "signal": "SIGKILL to the whole process group",
        "state_of_each_task_at_the_kill": stages_at_kill,
        "records_on_disk_at_the_kill": len(before), "of_them_unchanged_after_resume": len(before) - len(lost) - len(changed),
        "model_calls_before_the_kill": sum(sum(c.values()) for c in calls_before.values()),
        "model_calls_after_resume": sum(sum(c.values()) for c in calls_after.values()),
        "agent_runs_recorded_before_the_kill": len(attempts_before),
        "agent_runs_recorded_before_the_kill_and_kept": sum(1 for p in attempts_before if after.get(p) == before[p]),
        "ledger_events_before_the_kill": len(events_before), "ledger_events_after": len(events(run_dir)),
        "run_start_events": len(starts), "leftover_containers_removed": removed,
        "cost_before_the_kill_usd": cost_before, "cost_in_total_usd": cost(run_dir),
        "final_states": finals(run_dir), "seconds": round(time.time() - started),
    }


def two_copies(run_id: str, n: int, workers: int) -> dict:
    run_dir = ROOT / "runs" / run_id
    started = time.time()
    logs = [open(ROOT / "runs" / f"{run_id}-copy{i}.log", "a") for i in (1, 2)]
    procs = [subprocess.Popen(command(run_id, n, workers), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT) for log in logs]
    codes = [proc.wait() for proc in procs]
    problems = [f"copy {i} exited with code {code}" for i, code in enumerate(codes, 1) if code != 0]
    ledger = events(run_dir)
    workers_by_task: dict[str, set] = {}
    for event in ledger:
        if event.get("task_id") and event.get("stage") not in ("final",):
            workers_by_task.setdefault(event["task_id"], set()).add(event.get("pid"))
    shared = {task: sorted(pids) for task, pids in workers_by_task.items() if len(pids) > 1}
    if shared:
        problems.append(f"tasks worked on by both copies: {shared}")
    states = finals(run_dir)
    if len(states) != n:
        problems.append(f"{len(states)} tasks have records; {n} were asked for")
    unfinished = [task for task, final in states.items() if final == "no verdict"]
    problems += duplicated_steps(run_dir) + attempts_consistent(run_dir) + records_parse(run_dir)
    leftovers = [str(p.relative_to(run_dir)) for p in run_dir.rglob("*.tmp*") if "jobs" not in p.parts]
    if leftovers:
        problems.append(f"half-written files left behind: {leftovers[:5]}")
    shipped = [json.loads(p.read_text()).get("output") for p in run_dir.glob("tasks/*/state.json") if json.loads(p.read_text()).get("final") == "shipped"]
    if len(shipped) != len(set(shipped)):
        problems.append(f"two tasks were shipped to the same folder: {shipped}")
    tasks_per_copy = Counter(next(iter(pids)) for pids in workers_by_task.values() if len(pids) == 1)
    return {
        "test": "two-copies", "run_id": run_id, "passed": not problems, "problems": problems,
        "exit_codes": codes, "process_ids": [proc.pid for proc in procs],
        "tasks_done_by_each_copy": {str(pid): count for pid, count in tasks_per_copy.items()},
        "tasks": len(states), "tasks_left_without_a_verdict": unfinished,
        "ledger_events": len(ledger), "run_start_events": sum(e.get("stage") == "run" and e.get("outcome") == "start" for e in ledger),
        "model_calls": sum(sum(c.values()) for c in llm_calls(run_dir).values()),
        "cost_usd": cost(run_dir), "final_states": states, "seconds": round(time.time() - started),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("test", choices=("kill-resume", "two-copies"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--n", type=int, default=4)
    parser.add_argument("--workers", type=int, default=0, help="tasks at once per process (default: n for kill-resume, half of n for two-copies)")
    args = parser.parse_args()
    if (ROOT / "runs" / args.run_id).exists():
        sys.exit(f"runs/{args.run_id} already exists; use a new run id")
    if args.test == "kill-resume":
        result = kill_resume(args.run_id, args.n, args.workers or args.n)
    else:
        result = two_copies(args.run_id, args.n, args.workers or max(1, args.n // 2))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.run_id}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
