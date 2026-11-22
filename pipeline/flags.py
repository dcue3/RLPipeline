"""Flags: what the run audit and the fairness review found, for a person to read.

A task ships when 1 to 3 of its five valid runs pass. That is the definition of
learnable, and it is the whole of the shipping decision. The run
audit and the fairness review are model judgements; hand checks showed them
giving opposite verdicts on like cases and rejecting fair tasks, so they no
longer reject anything. What they find becomes a flag on the shipped task:

- a failed run the audit blames on the task;
- a pass the audit questions;
- a test the fairness review calls unfair, or one that reads the solver's source;
- a judge that gave no usable answer, so that part of the record is missing.

A flagged task has not been verified. It still needs a human read, and the
evidence folder and the report say so.

One thing here does still reject, because a program can decide it without
judgement: a passing run whose commands wrote to the grading files. A run
that only looked at them is flagged.
"""

import re
from pathlib import Path

from pipeline.analysis import load_steps, scan_commands
from pipeline.store import from_record_path, read_json

# Command-scan findings that show a run reaching into how the task is graded.
TAMPER_MEANINGS = ("touches the verifier's output", "touches the pre-installed grading tools", "writes over test tooling")
# Reaching in is only tampering if something is written. `find /opt/verifier`
# changes nothing and gains nothing (the tooling holds no tests and no answers):
# A task was rejected for exactly that command on a run that passed honestly.
GRADING_PATH = r"(/opt/verifier|/logs/verifier|reward\.(txt|json))"
WRITES_TO_GRADING = re.compile(
    rf"(>>?|\btee\b(\s+-a)?)\s*['\"]?\S*{GRADING_PATH}"  # redirected or tee'd into it
    rf"|\b(cp|mv|ln|install|rsync)\b[^\n;|&]*\s['\"]?\S*{GRADING_PATH}\S*\s*($|[;|&])"  # copied or moved onto it
    rf"|\b(rm|rmdir|chmod|chown|truncate|touch|mkdir|sed\s+-i|patch)\b[^\n;|&]*{GRADING_PATH}"  # removed or altered
)


def writes_to_grading(hit: dict) -> bool:
    """A scan hit that changes the grading files, not one that only looks at them."""
    if hit.get("meaning") == "writes over test tooling":
        return True
    return hit.get("meaning") in TAMPER_MEANINGS and bool(WRITES_TO_GRADING.search(hit.get("command") or ""))

KINDS = {
    "failed_run_blamed_on_task": "the run audit says a failed run was the task's fault, not the agent's",
    "pass_questioned": "the run audit says a passing run did not really solve the task",
    "pass_looked_for_grading_files": "a passing run ran a command that names the hidden tests, the reference solution or the grading tools, without changing them",
    "test_called_unfair": "the fairness review says a test expects something the agent could not have known",
    "test_inspects_source": "the fairness review says a test reads the solver's source instead of checking what it does",
    "no_run_audit": "the run audit gave no usable answer, so there is no explanation of the failed runs",
    "no_fairness_review": "the fairness review gave no usable answer",
}


def tampered_passes(tdir: Path, version: int, label: str = "band") -> list[dict]:
    """Passing runs whose commands touched the grading files. Decided by the command scan alone."""
    band_dir = Path(tdir) / label / f"v{version}"
    summary = read_json(band_dir / "summary.json") or {}
    found = []
    for number in summary.get("counted_attempts") or []:
        record = read_json(band_dir / f"a{number}.json") or {}
        if record.get("outcome") != "pass":
            continue
        trial_dir = (record.get("trial") or {}).get("trial_dir")
        hits = scan_commands(load_steps(from_record_path(trial_dir))) if trial_dir else []
        bad = [h for h in hits if writes_to_grading(h)]
        if bad:
            found.append({"attempt": number, "meaning": bad[0]["meaning"], "command": bad[0]["command"]})
    return found


def audit_flags(analysis: dict | None, unavailable: str = "") -> list[dict]:
    if analysis is None:
        return [{"source": "run_audit", "kind": "no_run_audit", "detail": unavailable[:300]}]
    flags = []
    for run in analysis.get("runs") or []:
        label, outcome = str(run.get("label") or ""), run.get("outcome")
        base = {"source": "run_audit", "attempt": run.get("attempt"), "label": label, "detail": str(run.get("explanation") or "")[:600]}
        if outcome == "fail" and label.startswith("task:"):
            flags.append({**base, "kind": "failed_run_blamed_on_task"})
        elif outcome == "pass" and label and label != "pass:legitimate":
            flags.append({**base, "kind": "pass_questioned"})
        elif outcome == "pass":
            looked = [h for h in run.get("scan_hits") or [] if not writes_to_grading(h)]
            if looked:
                flags.append({**base, "kind": "pass_looked_for_grading_files", "detail": f"{looked[0]['meaning']}: {looked[0]['command']}"[:600]})
    return flags


def fairness_flags(fairness: dict | None, unavailable: str = "") -> list[dict]:
    if fairness is None:
        return [{"source": "fairness_review", "kind": "no_fairness_review", "detail": unavailable[:300]}]
    flags = []
    for row in fairness.get("tests") or []:
        base = {"source": "fairness_review", "test": row.get("test"), "detail": str(row.get("issue") or row.get("checks") or "")[:600]}
        if row.get("inspects_implementation"):
            flags.append({**base, "kind": "test_inspects_source"})
        elif not row.get("fair"):
            flags.append({**base, "kind": "test_called_unfair"})
    return flags


def collect(analysis: dict | None, fairness: dict | None, *, no_audit: str = "", no_review: str = "") -> list[dict]:
    """Every flag for one task, run audit first."""
    return audit_flags(analysis, no_audit) + fairness_flags(fairness, no_review)


def failed_runs(analysis: dict | None) -> list[dict]:
    """The "why it failed" record: one entry per failed run, as the audit wrote it."""
    keys = ("attempt", "label", "explanation", "failed_requirement", "failed_tests", "evidence")
    return [{k: run.get(k) for k in keys} for run in (analysis or {}).get("runs") or [] if run.get("outcome") == "fail"]


def describe(flag: dict) -> str:
    """One line a reader can act on."""
    where = f"run {flag['attempt']}" if flag.get("attempt") else (flag.get("test") or "")
    label = f" ({flag['label']})" if flag.get("label") else ""
    detail = f": {flag['detail']}" if flag.get("detail") else ""
    return f"{KINDS.get(flag['kind'], flag['kind'])}{' - ' + where if where else ''}{label}{detail}"


def why_it_fails_markdown(name: str, passes: int, runs: int, analysis: dict | None, flags: list[dict]) -> str:
    """The page a reader opens first: is this task verified, and why did its failed runs fail."""
    lines = [f"# {name}: why it fails", "", f"{passes} of {runs} valid runs passed, so the task is in the 1 to 3 band and ships.", ""]
    if flags:
        lines += [
            f"**FLAGGED ({len(flags)}): not verified. This task still needs a human read.**",
            "",
            "The checks below are model judgements. They do not decide whether the task ships; they say where to look.",
            "",
            *[f"- {describe(flag)}" for flag in flags],
            "",
        ]
    else:
        lines += ["No flags: the run audit blamed every failed run on the agent, and the fairness review called every test fair.", ""]
    lines += ["## Each failed run", ""]
    failed = failed_runs(analysis)
    if analysis is None:
        lines.append("The run audit gave no usable answer, so the failed runs are not explained here. Read the trajectories.")
    elif not failed:
        lines.append("No failed run was recorded.")
    for run in failed:
        tests = ", ".join(run.get("failed_tests") or []) or "none recorded"
        lines += [
            f"### Run {run['attempt']}: {run['label']}",
            "",
            str(run.get("explanation") or "").strip(),
            "",
            f"- Failed tests: {tests}",
            *([f"- What it got wrong: {run['failed_requirement']}"] if run.get("failed_requirement") else []),
            *[f"- Evidence: {str(e)[:300]}" for e in (run.get("evidence") or [])[:3]],
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
