"""Build the image as soon as the container's files exist, and repair it with the real error.

Without this, an image that does not build is discovered only after every
other file has been written, at the functional gate, and the repair is shown
the last few thousand characters of Harbor's output. In earlier runs that report
named the failing step in 29 of 43 build failures and included a line the
step printed in 15; the repair worked about half the time and 14 of 100 tasks
were lost to builds.

Here the build is run directly, the failing step is picked out of Docker's own
output, and the repair is told the Dockerfile line, the exit code and what the
step printed. Nothing about the task's design changes.
"""

import hashlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from generator import author, config
from generator.llm import LLM
from pipeline.gates import InfrastructureError, network_unreachable
from pipeline.store import RunStore, atomic_write_json, atomic_write_text, read_json

_builds = threading.BoundedSemaphore(config.EARLY_BUILDS_AT_ONCE)

# Failures that are the machine's and never the Dockerfile's. Network errors inside a RUN step are
# not here: a build that downloads something it should not is the task's fault and can be repaired.
INFRASTRUCTURE = re.compile(r"no space left on device|cannot connect to the docker daemon|error during connect|toomanyrequests", re.IGNORECASE)
EXIT_CODES = {
    100: "apt-get failed, usually a package name that does not exist for this base image",
    126: "the command was found but could not be run (not executable, or wrong interpreter line)",
    127: "command not found: a program this step calls is not in the image at this point",
    137: "the step was killed, usually for using too much memory",
    139: "segmentation fault",
}
STEP_OUTPUT_CHARS = 3500


class BuildNeverPassed(Exception):
    """The image still does not build after every repair."""


def docker_build(context: Path, tag: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "build", "--progress=plain", "-t", tag, str(context)],
            capture_output=True, text=True, errors="replace", timeout=config.BUILD_TIMEOUT_SEC,
        )  # fmt: skip
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return False, partial + f"\nERROR: the build did not finish within {int(config.BUILD_TIMEOUT_SEC)} seconds"


def remove_image(tag: str) -> None:
    """The check needs the result, not the image. Docker keeps its build cache, so Harbor's own build is quick."""
    subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, text=True)


def summarise_failure(log: str, dockerfile: str) -> dict:
    """Pick the failing step out of a plain-progress build log.

    Returns {line, instruction, exit_code, output, error}. ``line`` is the
    Dockerfile line number Docker reports, or None.
    """
    error = ""
    for found in re.finditer(r"^(?:#\d+ )?ERROR:? (.*)$", log, re.MULTILINE):
        error = found.group(1).strip()
    exit_code = None
    code = re.search(r"exit code: (\d+)", error) or (list(re.finditer(r"exit code: (\d+)", log)) or [None])[-1]
    if code:
        exit_code = int(code.group(1))

    # "Dockerfile:9" followed by the lines around it, the failing one marked ">>>".
    line = None
    where = list(re.finditer(r"^Dockerfile:(\d+)\s*$", log, re.MULTILINE))
    if where:
        line = int(where[-1].group(1))
    marked = re.findall(r"^\s*\d+ \| >>> ?(.*)$", log, re.MULTILINE)
    instruction = " ".join(part.strip().rstrip("\\").strip() for part in marked) if marked else ""
    if not instruction and line:
        lines = dockerfile.splitlines()
        instruction = lines[line - 1].strip() if 0 < line <= len(lines) else ""

    # What the failing step printed: the lines of the step whose id carries the ERROR.
    output = ""
    step = (list(re.finditer(r"^#(\d+) ERROR", log, re.MULTILINE)) or [None])[-1]
    if step:
        number = step.group(1)
        printed = re.findall(rf"^#{number} \d+\.\d+ ?(.*)$", log, re.MULTILINE)
        output = "\n".join(printed)
        if not instruction:
            header = re.search(rf"^#{number} \[[^\]]*\] (.*)$", log, re.MULTILINE)
            instruction = header.group(1).strip() if header else ""
        if not output:
            # A step that printed nothing, such as a COPY of a file that is not there: Docker's
            # message for that step is all there is.
            own = re.findall(rf"^#{number} ERROR:? (.*)$", log, re.MULTILINE)
            output = "(nothing) Docker's message for this step: " + re.sub(r"ref \S+: ", "", own[-1]) if own else ""
    if not output:
        # No step could be identified: the end of the log
        # without the lines that only report layers being downloaded.
        kept = [l for l in log.splitlines() if not re.search(r"sha256:|extracting|^#\d+ (DONE|CACHED)|resolve docker.io|load metadata|transferring", l)]
        output = "\n".join(kept[-25:])
    error = re.sub(r"^(failed to build: )?(failed to solve: )?", "", error)
    return {"line": line, "instruction": instruction[:400], "exit_code": exit_code, "output": output[-STEP_OUTPUT_CHARS:].strip(), "error": error[:500]}


def failure_text(summary: dict, earlier: list[dict]) -> str:
    """What the repair is told. Everything in it comes from Docker's output for this build."""
    lines = ["The image did not build. This is what Docker reported."]
    where = f"Dockerfile line {summary['line']}: " if summary.get("line") else ""
    lines.append(f"Step that failed: {where}{summary.get('instruction') or '(not identified)'}")
    code = summary.get("exit_code")
    if code is not None:
        meaning = EXIT_CODES.get(code)
        lines.append(f"Exit code: {code}" + (f" ({meaning})" if meaning else ""))
    if summary.get("error"):
        lines.append(f"Docker's error: {summary['error']}")
    lines.append("What that step printed:\n" + (summary.get("output") or "(nothing)"))
    for number, attempt in enumerate(earlier, 1):
        lines.append(
            f"\nEarlier repair {number} changed {', '.join(attempt['changed']) or 'nothing'} and the build then failed at: "
            f"{attempt['instruction'] or '(not identified)'}"
            + (" (the same step as before)" if attempt.get("same_step") else "")
        )
    if earlier:
        lines.append("Do not repeat a change that has already been tried.")
    return "\n".join(lines)


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()


def _write_context(directory: Path, environment: dict[str, str]) -> Path:
    """The build context exactly as the task will ship it, with the pipeline's Dockerfile footer."""
    if directory.exists():
        shutil.rmtree(directory)
    for path, content in environment.items():
        atomic_write_text(directory / path, author._finish_file(path, content))
    return directory / "environment"


def _in_pipeline_footer(summary: dict, dockerfile: str) -> bool:
    """True if the step that failed is one the pipeline appended, not one the model wrote."""
    return bool(summary.get("line")) and summary["line"] > len(author._finish_file("environment/Dockerfile", dockerfile).split(author.DOCKERFILE_FOOTER_MARKER, 1)[0].splitlines())


def early_build(store: RunStore, task_id: str, tdir: Path, spec: dict, files: dict[str, str], llm: LLM, build=docker_build) -> dict[str, str]:
    """Build the container in ``files`` and repair it until it builds. Returns the files, with any repairs.

    Raises BuildNeverPassed when the repairs are used up, and InfrastructureError
    when the build failed for a reason that is the machine's (nothing is stored,
    so the task is tried again on the next run).
    """
    environment = {p: c for p, c in files.items() if p.startswith("environment/")}
    if "environment/Dockerfile" not in environment:
        return files
    record_path = tdir / "build_check.json"
    record = read_json(record_path) or {}
    given = _digest(environment)
    if record.get("input") == given and record.get("passed"):
        return {**files, **record["files"]}  # this container was already built, perhaps after repairs

    attempts: list[dict] = []
    earlier: list[dict] = []
    tag = re.sub(r"[^a-z0-9_.-]", "-", f"rlp-buildcheck-{store.run_dir.name}-{task_id}".lower())
    for number in range(1, config.MAX_EARLY_BUILD_REPAIRS + 2):
        context = _write_context(tdir / "build_check" / f"a{number}", environment)
        started = time.time()
        with _builds:
            ok, log = build(context, tag)
            remove_image(tag)
        seconds = round(time.time() - started, 1)
        atomic_write_text(context.parent / "build.log", log[-200000:])
        if ok:
            attempts.append({"attempt": number, "passed": True, "seconds": seconds})
            store.log_event(task_id, "build_check", "pass", attempt=number, duration_sec=seconds, repairs=number - 1)
            atomic_write_json(record_path, {"input": given, "passed": True, "attempts": attempts, "files": environment})
            return {**files, **environment}

        summary = summarise_failure(log, author._finish_file("environment/Dockerfile", environment["environment/Dockerfile"]))
        if INFRASTRUCTURE.search(log[-6000:]) or network_unreachable(log[-12000:]):
            raise InfrastructureError(f"early build failed for a reason that is not the task's: {summary['error'] or log[-300:]}")
        if _in_pipeline_footer(summary, environment["environment/Dockerfile"]):
            # The step that failed is one the pipeline appends (the verifier's tooling). The author cannot
            # see or repair it here, so this check says nothing and the functional gate decides.
            store.log_event(task_id, "build_check", "skipped", attempt=number, reason="the failing step is the pipeline's own: " + summary["instruction"][:120])
            return {**files, **environment}
        # Docker counts lines in the file as built, which has the pipeline's canary line on top.
        # The author is shown its own file, so give the line number in that.
        model_file = environment["environment/Dockerfile"]
        added_on_top = author._finish_file("environment/Dockerfile", model_file).find(model_file.splitlines()[0]) if model_file.strip() else 0
        if summary["line"] and added_on_top > 0:
            summary["line"] -= author._finish_file("environment/Dockerfile", model_file)[:added_on_top].count("\n")
        attempts.append({"attempt": number, "passed": False, "seconds": seconds, **summary})
        store.log_event(
            task_id, "build_check", "fail", attempt=number, duration_sec=seconds, line=summary["line"],
            step=summary["instruction"][:160], exit_code=summary["exit_code"], evidence=str((context.parent / "build.log").relative_to(store.run_dir)),
        )  # fmt: skip
        if number > config.MAX_EARLY_BUILD_REPAIRS:
            atomic_write_json(record_path, {"input": given, "passed": False, "attempts": attempts})
            raise BuildNeverPassed(
                f"the image did not build after {config.MAX_EARLY_BUILD_REPAIRS} repairs; last failure at "
                f"{summary['instruction'][:160] or 'an unidentified step'} (exit code {summary['exit_code']})"
            )
        if earlier:
            earlier[-1]["instruction"] = summary["instruction"]
            earlier[-1]["same_step"] = summary["instruction"] == attempts[-2].get("instruction")
        changed = author.write_build_repair(llm, spec, environment, failure_text(summary, earlier), tdir / "llm")
        environment.update(changed)
        earlier.append({"changed": sorted(changed), "instruction": "", "same_step": False})
        store.log_event(task_id, "build_repair", "ok", attempt=number, changed=sorted(changed))
    raise AssertionError("unreachable")
