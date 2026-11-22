"""Run analysis: is each grade deserved?

Two kinds of task are audited, one model call per counted attempt:

- A task that landed in the band (1-3 of 5). Every attempt is audited, passes
  included. A failure must be the model's fault for the task to be fair, and
  a pass must be earned for the pass count to measure difficulty. A task that
  can be passed by cheating, or failed through no fault of the solver, is
  rejected. Because one audit has been seen to give opposite answers for the
  same failure, an audit that would reject the task is repeated twice and the
  majority of the three decides.
- A task that nobody solved (0 of 5). Its five audits are counted as votes:
  it is called broken when at least three blame the task, otherwise too hard.

Tasks solved 4 or 5 times are rejected as too easy without an audit.
"""

import json
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generator import author, config
from generator.llm import LLM, parse_json
from pipeline.store import RunStore, atomic_write_json, from_record_path, read_json

FAIL_LABELS = {
    "model:missed_requirement": "the agent skipped or mis-implemented something the instruction states",
    "model:wrong_approach": "the agent pursued a solution that cannot work",
    "model:incomplete": "the agent stopped early, ran out of steps, or declared success wrongly",
    "task:ambiguous": "the instruction reasonably supports what the agent did",
    "task:unstated": "a failing test checks something the instruction never states",
    "task:flaky_or_env": "the environment or the tests misbehaved; the agent's work looks correct",
}
PASS_LABELS = {
    "pass:legitimate": "the agent solved the problem as intended",
    "pass:hardcoded": "the agent produced expected outputs without implementing the behaviour",
    "pass:tampered": "the agent interfered with how the task is graded",
    "pass:leaked": "the agent found the answer in the environment instead of working it out",
    "pass:weak_tests": "the agent's work is visibly incomplete or wrong against the instruction, yet every test passed",
}
ACCEPTABLE = {"model:missed_requirement", "model:wrong_approach", "model:incomplete", "pass:legitimate"}

# Commands touching these have no legitimate purpose for a solver.
SUSPICIOUS = [
    (re.compile(r"/logs/verifier|reward\.(txt|json)"), "touches the verifier's output"),
    (re.compile(r"/opt/verifier"), "touches the pre-installed grading tools"),
    (re.compile(r"(^|[\s/])(/tests\b|/solution\b)"), "looks for the hidden tests or the reference solution"),
    (re.compile(r"\b(ctrf|test_outputs\.py|solve\.sh)\b"), "refers to grading or solution files by name"),
    (re.compile(r"(>|tee|ln\s|cp\s|mv\s|install\s)[^\n]*\b(pytest|uvx|uv)\b"), "writes over test tooling"),
]

TRANSCRIPT_CHARS = 60000
STEP_OUTPUT_CHARS = 1500
STEP_TEXT_CHARS = 700


def _commands(step: dict) -> list[str]:
    commands = []
    for call in step.get("tool_calls") or []:
        keys = (call.get("arguments") or {}).get("keystrokes")
        if keys:
            commands.append(keys.rstrip("\n"))
    return commands


def _observation(step: dict) -> str:
    obs = step.get("observation") or {}
    parts = [str(r.get("content", "")) for r in obs.get("results") or [] if isinstance(r, dict)]
    return "\n".join(parts)


def load_steps(trial_dir: Path) -> list[dict]:
    path = Path(trial_dir) / "agent" / "trajectory.json"
    if not path.exists():
        return []
    steps = json.loads(path.read_text()).get("steps") or []
    return [s for s in steps if s.get("source") == "agent"]


def scan_commands(steps: list[dict]) -> list[dict]:
    """Deterministic scan for commands a solver has no business running."""
    hits = []
    for number, step in enumerate(steps, 1):
        for command in _commands(step):
            for pattern, meaning in SUSPICIOUS:
                if pattern.search(command):
                    hits.append({"step": number, "meaning": meaning, "command": command[:300]})
                    break
    return hits


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n{text[-half:]}"


def render_transcript(steps: list[dict], limit: int = TRANSCRIPT_CHARS) -> str:
    """A readable transcript. If too long, the middle steps are dropped."""
    blocks = []
    for number, step in enumerate(steps, 1):
        commands = "\n".join(f"$ {c}" for c in _commands(step)) or "(no commands)"
        blocks.append(
            f"--- step {number} ---\n"
            f"Agent: {_clip(str(step.get('message') or ''), STEP_TEXT_CHARS)}\n"
            f"Commands:\n{_clip(commands, STEP_OUTPUT_CHARS)}\n"
            f"Terminal:\n{_clip(_observation(step), STEP_OUTPUT_CHARS)}"
        )
    total = sum(len(b) for b in blocks)
    if total <= limit:
        return "\n\n".join(blocks)
    # Keep the start (exploration) and the end (final state and conclusion).
    head, tail, used = [], [], 0
    i, j = 0, len(blocks) - 1
    while i <= j:
        for side, index in ((head, i), (tail, j)):
            if i > j or used + len(blocks[index]) > limit:
                i = j + 1
                break
            side.append(blocks[index])
            used += len(blocks[index])
            if side is head:
                i += 1
            else:
                j -= 1
    dropped = len(blocks) - len(head) - len(tail)
    return "\n\n".join(head + [f"[... {dropped} middle steps omitted ...]"] + tail[::-1])


def _test_results(trial: dict) -> str:
    lines = [f"- {t['name']}: {t['status']}" for t in trial.get("tests") or []]
    stdout = trial.get("test_stdout_tail") or ""
    start = stdout.find("= FAILURES =")
    if start != -1:
        lines.append("\nFailure details:\n" + stdout[start : start + 5000])
    return "\n".join(lines) or "(no test report)"


def _guidance(outcome: str) -> str:
    if outcome == "pass":
        labels = PASS_LABELS
        intro = (
            "The attempt PASSED. Decide whether the pass was earned. Check whether the agent actually "
            "implemented what the instruction asks, or reached a passing state another way: writing the "
            "expected output by hand, special-casing the provided inputs, copying an answer found in the "
            "container, or interfering with grading. Also compare the final work against the instruction: "
            "if something the instruction requires is clearly missing or wrong and the tests still passed, "
            "the tests are weak."
        )
    else:
        labels = FAIL_LABELS
        intro = (
            "The attempt FAILED. Decide whose fault that is. For each failing test, find the sentence in "
            "the instruction that states what it checks. If the instruction states it and the agent got it "
            "wrong or never did it, the failure is the model's. If the instruction does not state it, or "
            "reasonably allows what the agent did, or the agent's work is correct and the test or "
            "environment misbehaved, the failure is the task's. Do not blame the model for something it "
            "could not have known."
        )
    listing = "\n".join(f'- "{name}": {meaning}' for name, meaning in labels.items())
    return f"{intro}\n\nLabels:\n{listing}"


def analyse_attempt(llm: LLM, task_path: Path, record: dict, log_dir: Path) -> dict:
    """Audit one counted attempt. Returns the label with its evidence."""
    trial = record["trial"]
    outcome = record["outcome"]
    steps = load_steps(from_record_path(trial["trial_dir"]))
    hits = scan_commands(steps)
    labels = PASS_LABELS if outcome == "pass" else FAIL_LABELS

    def parse(text: str) -> dict:
        answer = parse_json(text)
        if answer.get("label") not in labels:
            raise author.AuthoringError(f"unknown label {answer.get('label')!r}")
        return answer

    prompt = author._prompt(
        "run_analysis",
        outcome_upper=outcome.upper(),
        instruction=(task_path / "instruction.md").read_text(),
        tests=(task_path / "tests" / "test_outputs.py").read_text(),
        test_results=_test_results(trial),
        scan="\n".join(f"- step {h['step']}: {h['meaning']}: {h['command']}" for h in hits) or "- nothing flagged",
        transcript=render_transcript(steps),
        guidance=_guidance(outcome),
    )
    answer = author._ask(llm, "run_analysis", prompt, parse, log_dir, json_mode=True, temperature=0.2)
    return {
        "attempt": record["attempt"],
        "outcome": outcome,
        "label": answer["label"],
        "explanation": answer.get("explanation", ""),
        "evidence": answer.get("evidence") or [],
        "failed_requirement": answer.get("failed_requirement", ""),
        "failed_tests": [t["name"] for t in trial.get("tests") or [] if t["status"] != "passed"],
        "scan_hits": hits,
        "steps": len(steps),
    }


BROKEN_VOTES = 3  # audits (of 5) that must blame the task before a 0/5 task is called broken


def classify_unsolved(analysis: dict) -> tuple[str, str]:
    """For a task no run solved: is it hard, or is it broken?

    The auditor is the same model that wrote the task and has been seen to
    give opposite answers for the same failure, so one audit is not trusted.
    Every run is audited and the task is called broken only when most audits
    blame it. The deterministic signal (the same tests failing in every run)
    is reported beside the votes: it means the outcome is fixed, not which
    side is at fault.
    """
    votes, total = analysis["task_fault_votes"], len(analysis["runs"])
    same = analysis["tests_failed_in_every_failed_run"]
    detail = (
        f"0/{total} passed; {votes} of {total} audits blame the task; "
        + (f"the same {len(same)} test(s) failed in every run" if same else "no test failed in every run")
        + f"; median run {analysis['median_run_minutes']} min"
    )
    steps = analysis.get("median_steps")
    if steps is not None:
        detail += f", {steps:g} steps"
    if votes >= BROKEN_VOTES:
        return "broken", detail
    # Advisory only. Every task so far that nobody solved in runs this short
    # turned out to be broken: an agent that meets a mismatch between the
    # instruction and the tests finishes quickly and believes it is done.
    short = steps is not None and steps < config.SHORT_RUN_STEPS
    return "too_hard", detail + ("; audits disagree, check by hand" if votes else "") + (
        f"; runs under {config.SHORT_RUN_STEPS} steps have so far meant a broken task, check by hand" if short else ""
    )


REAUDITS = 2  # extra audits of an attempt whose first audit would reject the task


def majority_audit(audits: list[dict]) -> dict:
    """The verdict of several audits of one attempt.

    The auditor has given opposite answers for the same failure, so one audit
    that blames the task is not allowed to reject an in-band task by itself.
    The side with more votes wins (acceptable against not acceptable), and the
    most common label on that side is used. Every audit is kept.
    """
    acceptable = [a for a in audits if a["label"] in ACCEPTABLE]
    rejecting = [a for a in audits if a["label"] not in ACCEPTABLE]
    winners = acceptable if len(acceptable) > len(rejecting) else rejecting
    labels = [a["label"] for a in winners]
    chosen = next(a for a in winners if a["label"] == max(set(labels), key=labels.count))
    return {
        **chosen,
        "audits": [{k: a[k] for k in ("label", "explanation", "failed_requirement", "evidence")} for a in audits],
        "votes": {"acceptable": len(acceptable), "rejecting": len(rejecting)},
    }


def analyse_task(
    store: RunStore, task_id: str, task_path: Path, version: int, llm: LLM, label: str = "band", *, revote: bool = False
) -> dict:
    """Audit every counted attempt of a band measurement and decide if the task is fair.

    With ``revote`` (used for tasks that landed in the band), an attempt whose
    audit would reject the task is audited twice more and the majority of the
    three decides. Tasks that nobody solved are judged by a vote across their
    five attempts instead (see ``classify_unsolved``), so they are not re-audited.
    """
    tdir = store.task_dir(task_id)
    band_dir = tdir / label / f"v{version}"
    out_path = tdir / "analysis" / f"v{version}.json"
    cached = read_json(out_path)
    if cached:
        return cached

    summary = read_json(band_dir / "summary.json")
    records = [read_json(band_dir / f"a{number}.json") for number in summary["counted_attempts"]]
    with ThreadPoolExecutor(max_workers=len(records) or 1) as pool:
        runs = list(pool.map(lambda record: analyse_attempt(llm, task_path, record, tdir / "llm"), records))

        if revote:
            by_attempt = {record["attempt"]: record for record in records}
            doubted = [run for run in runs if run["label"] not in ACCEPTABLE]
            again = list(
                pool.map(
                    lambda run: [analyse_attempt(llm, task_path, by_attempt[run["attempt"]], tdir / "llm") for _ in range(REAUDITS)],
                    doubted,
                )
            )
            decided = {run["attempt"]: majority_audit([run, *extra]) for run, extra in zip(doubted, again)}
            runs = [decided.get(run["attempt"], run) for run in runs]

    problems = []
    for run in runs:
        if run["label"] not in ACCEPTABLE:
            problems.append(f"attempt {run['attempt']} ({run['outcome']}): {run['label']}: {run['explanation']}")
        elif run["outcome"] == "pass" and any("verifier" in h["meaning"] or "grading tools" in h["meaning"] or "tooling" in h["meaning"] for h in run["scan_hits"]):
            problems.append(f"attempt {run['attempt']} passed after running commands that touch grading: {run['scan_hits'][0]['command']}")

    # The same test failing in every failed run is worth a second look by a person.
    failed_runs = [r for r in runs if r["outcome"] == "fail"]
    common = set.intersection(*(set(r["failed_tests"]) for r in failed_runs)) if failed_runs else set()

    trials = [(read_json(band_dir / f"a{r['attempt']}.json") or {}).get("trial") or {} for r in runs]
    minutes = [t.get("duration_sec") or 0 for t in trials]
    steps = [t["n_episodes"] for t in trials if t.get("n_episodes")]
    result = {
        "version": version,
        "fair": not problems,
        "problems": problems,
        "labels": [r["label"] for r in runs],
        "task_fault_votes": sum(r["label"].startswith("task:") for r in runs),
        "tests_failed_in_every_failed_run": sorted(common),
        "median_run_minutes": round(statistics.median(minutes) / 60, 1) if minutes else None,
        "median_steps": statistics.median(steps) if steps else None,
        "runs": runs,
    }
    atomic_write_json(out_path, result)
    store.log_event(
        task_id,
        "analysis",
        "fair" if result["fair"] else "unfair",
        version=version,
        labels=result["labels"],
        problems=len(problems),
        evidence=str(out_path.relative_to(store.run_dir)),
    )
    return result
