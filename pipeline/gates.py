"""Gates a candidate must pass before any agent evaluation is spent on it.

Structural: the validator in validator/ plus a few static checks of our own.
Functional: the reference solution passes every test (oracle, run twice to
catch flaky tests) and an untouched container fails (nop).
"""

import ast
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from generator import config
from pipeline.answer_keys import answer_key_findings
from pipeline.harbor_runner import TrialResult, run_job
from validator.validate import validate_task

ORACLE_RUNS = 2
GATE_TIMEOUT_SEC = 1800  # build + solve + verify; far above anything reasonable


@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str = ""
    failure_report: str = ""  # shown to the model when asking for a repair
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --- Structural ------------------------------------------------------------


def structural_gate(task_dir: Path, base_image: str | None = None) -> GateResult:
    """Layout, schema, Dockerfile, instruction and answer-key checks on a task directory."""
    result = validate_task(str(task_dir))
    errors = list(result["errors"])

    dockerfile = task_dir / "environment" / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text()
        from_lines = [l.split()[1] for l in text.splitlines() if l.strip().upper().startswith("FROM ") and len(l.split()) > 1]
        if base_image and from_lines and from_lines[0] != base_image:
            errors.append(f"Dockerfile must start FROM {base_image}, found {from_lines[0]}.")
        for banned in ("solution/", "solve.sh", "test_outputs", "ENTRYPOINT"):
            if banned in text:
                errors.append(f"Dockerfile must not reference {banned!r}.")
        # Tests and solve.sh assume /app. The image's working directory is its
        # last WORKDIR, so that must be /app.
        workdirs = [l.split(None, 1)[1].strip().strip('"') for l in text.splitlines() if l.strip().upper().startswith("WORKDIR ") and len(l.split(None, 1)) > 1]
        if not workdirs or workdirs[-1].rstrip("/") != "/app":
            errors.append(
                f"The Dockerfile's last WORKDIR must be /app (found {workdirs[-1] if workdirs else 'none'}). "
                "Use WORKDIR elsewhere only temporarily and end with `WORKDIR /app`."
            )

    tests = task_dir / "tests" / "test_outputs.py"
    if not tests.exists():
        errors.append("tests/test_outputs.py is missing.")
    else:
        tests_text = tests.read_text()
        # The reference solution is not in the container when an agent is
        # graded, so a test that runs or reads it can only pass for the oracle.
        for banned in ("solve.sh", "/solution"):
            if banned in tests_text:
                errors.append(
                    f"tests/test_outputs.py must not refer to {banned!r}: the reference solution "
                    "is not present when the tests grade an agent's work."
                )
        try:
            tree = ast.parse(tests_text)
            n_tests = sum(
                isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                for node in ast.walk(tree)
            )
            if n_tests < 1:
                errors.append("tests/test_outputs.py has no test functions.")
        except SyntaxError as exc:
            errors.append(f"tests/test_outputs.py has a syntax error: {exc}")

    solve = task_dir / "solution" / "solve.sh"
    if solve.exists():
        check = subprocess.run(["bash", "-n", str(solve)], capture_output=True, text=True)
        if check.returncode != 0:
            errors.append(f"solution/solve.sh has a shell syntax error: {check.stderr.strip()[:500]}")

    instruction = task_dir / "instruction.md"
    if instruction.exists():
        text = instruction.read_text()
        lowered = text.lower()
        for leak in ("test_outputs", "solve.sh", "/tests/"):
            if leak in lowered:
                errors.append(f"instruction.md must not mention {leak!r}.")
        words = len(text.split())
        limit = config.INSTRUCTION_MAX_WORDS
        if words > limit:
            errors.append(f"instruction.md is {words} words; the limit is {limit}. State the goal and the symptoms, not every rule.")

    # The container must not ship its own answers: no generator left where the
    # solver can read it, and no file holding the expected result.
    answer_keys = answer_key_findings(task_dir)
    errors.extend(finding["message"] for finding in answer_keys)

    passed = not errors
    return GateResult(
        gate="structural",
        passed=passed,
        reason="ok" if passed else f"{len(errors)} structural error(s)",
        failure_report="" if passed else "Structural validation failed:\n" + "\n".join(f"- {e}" for e in errors),
        details={"errors": errors, "warnings": result["warnings"], "answer_keys": answer_keys},
    )


# --- Functional ------------------------------------------------------------


def _failure_section(stdout: str, limit: int = 7000) -> str:
    """The part of pytest's output that explains failures, not the pass list."""
    for marker in ("= FAILURES =", "= ERRORS ="):
        start = stdout.find(marker)
        if start != -1:
            return stdout[start : start + limit]
    return stdout[-3500:]


def _describe_trial(label: str, trial: TrialResult) -> str:
    lines = [f"{label}: reward={trial.reward}"]
    if trial.exception_type:
        lines.append(
            f"Harbor raised {trial.exception_type} before the tests could run. "
            f"End of its message:\n{(trial.exception_message or '')[-2500:]}"
        )
    if trial.tests:
        failed = trial.failed_tests
        lines.append(f"{len(trial.tests) - len(failed)} test(s) passed, {len(failed)} did not:")
        lines += [f"- {test['name']} [{test['status']}]" for test in failed[:20]]
    if trial.test_stdout_tail:
        lines.append("Test output:\n" + _failure_section(trial.test_stdout_tail))
    return "\n".join(lines)


TEST_SECTION = re.compile(r"^_{3,} (.+?) _{3,}$", re.MULTILINE)
REPORT_CHARS = 16000  # the whole report of an oracle failure
SOLVE_OUTPUT_CHARS = 2500


def failed_test_sections(stdout: str) -> list[tuple[str, str]]:
    """(heading, text) for every failure pytest wrote up, in order. Empty if there is no failures section."""
    starts = [stdout.find(marker) for marker in ("= FAILURES =", "= ERRORS =") if marker in stdout]
    if not starts:
        return []
    body = stdout[min(starts) :]
    end = re.search(r"^=+ (short test summary info|warnings summary) =+$", body, re.MULTILINE)
    if end:
        body = body[: end.start()]
    found = list(TEST_SECTION.finditer(body))
    return [(m.group(1).strip(), body[m.end() : (found[i + 1].start() if i + 1 < len(found) else len(body))].strip()) for i, m in enumerate(found)]


def _shorten(text: str, limit: int) -> str:
    """Keep the start (the test) and more of the end (the assertion and what it compared)."""
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + "\n   [...]\n" + text[-(limit - head) :]


def describe_oracle_failure(label: str, trial: TrialResult) -> str:
    """Everything the records hold about why an oracle run failed (design feature full-reports).

    The short report gives the names of the failed tests and the end of
    pytest's output. Missing from it: what solve.sh itself printed
    and its exit code (it had crashed in at least 34 of 98 failures), and the
    detail of 136 of 358 failed tests.
    """
    from pipeline.store import from_record_path  # here to keep this module's imports as they were

    trial_dir = from_record_path(trial.trial_dir)
    lines = [f"{label}: reward={trial.reward}"]
    if trial.exception_type:
        lines.append(f"Harbor raised {trial.exception_type} before the tests could run. End of its message:\n{(trial.exception_message or '')[-2500:]}")

    # Harbor writes the exit code only when it is not zero.
    code_path, output_path = trial_dir / "agent" / "exit-code.txt", trial_dir / "agent" / "oracle.txt"
    code = code_path.read_text().strip() if code_path.exists() else "0"
    printed = output_path.read_text(errors="replace").strip() if output_path.exists() else ""
    if code != "0":
        lines.append(
            f"solve.sh itself FAILED: it exited with code {code}. Under `set -e` it stopped at the command that failed, so "
            "nothing after that command ran. Fix this first; most or all of the test failures below follow from it."
        )
    else:
        lines.append("solve.sh ran to the end (exit code 0).")
    lines.append("What solve.sh printed" + (f" (the last {SOLVE_OUTPUT_CHARS} characters)" if len(printed) > SOLVE_OUTPUT_CHARS else "") + ":\n" + (printed[-SOLVE_OUTPUT_CHARS:] or "(nothing)"))

    failed = trial.failed_tests
    if trial.tests:
        lines.append(f"{len(trial.tests) - len(failed)} test(s) passed, {len(failed)} did not:")
        lines += [f"- {test['name']} [{test['status']}]" for test in failed[:30]]

    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else trial.test_stdout_tail
    sections = failed_test_sections(stdout)
    if sections:
        used = sum(len(line) for line in lines)
        each = max(700, (REPORT_CHARS - used) // len(sections))
        lines.append("What each failed test reported:")
        lines += [f"--- {heading} ---\n{_shorten(text, each)}" for heading, text in sections]
    elif stdout.strip():
        lines.append("Test output (it has no failures section; this is its end):\n" + stdout[-3500:])
    return "\n".join(lines)


def _errored(trial: TrialResult) -> bool:
    """Harbor could not run the task at all (no reward was produced)."""
    return trial.reward is None


def _docker_out_of_networks(message: str) -> bool:
    """Docker gives every trial its own network and has about 31 to hand out.

    When other work on the machine holds them, a trial cannot start. That says
    nothing about the task.
    """
    message = message.lower()
    return "address pool" in message or "failed to create network" in message


def _mentions_build_failure(text: str) -> bool:
    text = text.lower()
    return any(marker in text for marker in ("dockerfile", "failed to solve", "did not complete successfully"))


def _is_infrastructure_failure(trial: TrialResult) -> bool:
    """The trial says nothing about the task and is worth one retry.

    A failed image build is the task's fault, so it is not retried. A verifier
    that produced no test report usually means its tooling could not be
    downloaded.
    """
    if _errored(trial):
        message = (trial.exception_message or "").lower()
        if _docker_out_of_networks(message):
            return True
        return not any(word in message for word in ("dockerfile", "build", "exit code"))
    return trial.reward == 0.0 and not trial.has_test_report and "pytest" not in trial.test_stdout_tail


class InfrastructureError(RuntimeError):
    """A gate could not run for a reason that says nothing about the task."""


def _docker_out_of_space(text: str) -> bool:
    """Docker's own disk is full. Every build fails then, whatever the task.

    Seen when 30 tasks were built at once: the first 25 functional checks all
    "failed to build", 13 were sent for a repair they did not need and 4 were
    rejected, before anyone looked.
    """
    lowered = text.lower()
    return "no space left on device" in lowered or "resourceexhausted" in lowered


# A build step could not reach the outside world: apt, pip, npm, go or curl
# found no route to a host or could not resolve it. That is the network's
# state at that moment, not something the task's files did.
NETWORK_UNREACHABLE = re.compile(
    r"could not connect to [\w.\-]+|unable to connect to [\w.\-]+|temporary failure (in name resolution|resolving)"
    r"|could not resolve host|failed to connect to [\w.\-]+ port \d+|network is unreachable"
    r"|connection timed out|dial tcp[^\n]*(i/o timeout|no route to host)|getaddrinfo (eai_again|enotfound)",
    re.IGNORECASE,
)


# The hosts every task's build depends on: the package indexes and the place the
# verifier's tooling comes from. A step that cannot reach one of these has met
# an outage. A step that cannot reach some other host is the task's own doing
# (a Dockerfile that downloads something it should not), and is repaired.
PACKAGE_HOSTS = re.compile(
    r"deb\.debian\.org|security\.debian\.org|debian\.map\.fastlydns\.net|archive\.ubuntu\.com|astral\.sh|pypi\.org"
    r"|files\.pythonhosted\.org|registry\.npmjs\.org|proxy\.golang\.org|sum\.golang\.org|github\.com|githubusercontent\.com",
    re.IGNORECASE,
)


def network_unreachable(text: str) -> bool:
    """A build could not reach a package index or the verifier's tooling. Every build fails then, whatever the task.

    During one earlier run, for about 25 minutes, Docker builds could not
    reach deb.debian.org or astral.sh (the model gateway stayed reachable). Two
    batches ran through it: 8 of their first 9 finished tasks were rejected
    as "environment did not build", after repairs that had nothing to repair.
    """
    return any(NETWORK_UNREACHABLE.search(line) and PACKAGE_HOSTS.search(line) for line in (text or "").splitlines())


def _infrastructure_reason(oracle_trials: list[TrialResult], nop_trials: list[TrialResult], output: str) -> str | None:
    """Why this gate run tells us nothing about the task, or None if it does."""
    trials = oracle_trials + nop_trials
    if _docker_out_of_space(output) or any(_docker_out_of_space(t.exception_message or "") for t in trials):
        return "Docker has no disk space left (free its build cache or raise its disk limit)"
    if network_unreachable(output) or any(network_unreachable(t.exception_message or "") for t in trials):
        return "a build step could not reach the network (apt, pip or the verifier's tooling could not connect)"
    for trial in trials:
        if _docker_out_of_networks(trial.exception_message or ""):
            return "Docker had no free network for the trial"
    if len(oracle_trials) < ORACLE_RUNS or len(nop_trials) < 1:
        if _docker_out_of_networks(output):
            return "Docker had no free network for the trial"
        if _mentions_build_failure(output):
            return None  # the image did not build: that is the task's fault
        return "Harbor returned no result: " + " ".join(output.split())[-300:]
    for trial in trials:
        if _is_infrastructure_failure(trial):
            if _errored(trial):
                return f"Harbor failed before the tests ran ({trial.exception_type}): " + " ".join((trial.exception_message or "").split())[-300:]
            return "the verifier's tooling did not run (no test report was produced)"
    return None


def functional_gate(task_dir: Path, jobs_dir: Path, tag: str) -> GateResult:
    """Oracle twice and nop once, in parallel. ``tag`` makes job names unique."""

    def oracle():
        return run_job(task_dir, "oracle", jobs_dir, f"{tag}-oracle", n_attempts=ORACLE_RUNS, timeout_sec=GATE_TIMEOUT_SEC)

    def nop():
        return run_job(task_dir, "nop", jobs_dir, f"{tag}-nop", timeout_sec=GATE_TIMEOUT_SEC)

    # A failure that says nothing about the task (Docker out of networks, Harbor
    # returning no result, the verifier's tooling never running) is retried with
    # a growing wait. If it persists, the gate does not return a verdict at all:
    # a verdict would be cached, would use up the task's one functional repair
    # (asking the model to "fix" a Docker error) and could reject a good task.
    waits = (0, *config.INFRA_RETRY_WAITS_SEC)
    for attempt, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        with ThreadPoolExecutor(max_workers=2) as pool:
            oracle_future, nop_future = pool.submit(oracle), pool.submit(nop)
            (oracle_trials, oracle_out), (nop_trials, nop_out) = oracle_future.result(), nop_future.result()
        trials = oracle_trials + nop_trials
        infra_reason = _infrastructure_reason(oracle_trials, nop_trials, oracle_out + "\n" + nop_out)
        if infra_reason is None:
            break
    else:
        raise InfrastructureError(f"{infra_reason} (after {len(waits)} tries)")

    details = {
        "oracle": [t.to_dict() for t in oracle_trials],
        "nop": [t.to_dict() for t in nop_trials],
        "duration_sec": sum(t.duration_sec or 0 for t in trials),
        "infrastructure_retries": attempt,
    }

    if len(oracle_trials) < ORACLE_RUNS or len(nop_trials) < 1:
        return GateResult(
            gate="functional",
            passed=False,
            reason="environment did not build or start",
            failure_report="Harbor could not build or start the task. Output:\n" + (oracle_out + "\n" + nop_out)[-3500:],
            details=details,
        )

    problems: list[str] = []
    reasons: list[str] = []

    errored = [t for t in trials if _errored(t)]
    if errored:
        return GateResult(
            gate="functional",
            passed=False,
            reason="environment did not build or start",
            failure_report=_describe_trial("The task could not be run", errored[0]),
            details=details,
        )

    for number, trial in enumerate(oracle_trials, 1):
        if not trial.passed:
            reasons.append("oracle failed")
            describe = describe_oracle_failure if config.design_has("full-reports") else _describe_trial
            problems.append(describe(f"ORACLE run {number} (solve.sh then tests) did not pass", trial))
            break  # one failing oracle report is enough for a repair
    oracle_ok = all(t.passed for t in oracle_trials)
    if not oracle_ok and any(t.passed for t in oracle_trials):
        reasons.append("oracle is flaky")
        problems.append("The oracle passed in one run and failed in another: something is non-deterministic.")

    nop_trial = nop_trials[0]
    if nop_trial.reward != 0.0:
        reasons.append("nop did not fail")
        problems.append(
            "NOP: the tests PASSED on the untouched container "
            f"(reward={nop_trial.reward}). The tests do not detect an unsolved task."
        )
    elif oracle_ok and nop_trial.tests:
        n_fail = len(nop_trial.failed_tests)
        details["nop_failed_tests"] = n_fail
        details["nop_total_tests"] = len(nop_trial.tests)

    passed = not problems
    return GateResult(
        gate="functional",
        passed=passed,
        reason="ok" if passed else "; ".join(dict.fromkeys(reasons)),
        failure_report="\n\n".join(problems),
        details=details,
    )
