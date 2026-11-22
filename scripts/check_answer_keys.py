#!/usr/bin/env python3
"""Run the answer-key checks over tasks that were generated earlier.

    python scripts/check_answer_keys.py <runs dir> [run ...]

For every task under ``<runs dir>/<run>/tasks/``, checks the final version
(the highest ``vN``) for a generator left where the solver can read it, a file
holding the expected result, and an instruction that points at such a file.
Nothing is built or run: only file names and the Dockerfile's text are read.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.answer_keys import answer_key_findings  # noqa: E402


def final_version(task_dir: Path) -> Path | None:
    versions = sorted(
        (int(re.fullmatch(r"v(\d+)", d.name).group(1)), d)
        for d in task_dir.iterdir()
        if d.is_dir() and re.fullmatch(r"v\d+", d.name)
    )
    for _, version_dir in reversed(versions):
        for candidate in sorted(version_dir.iterdir()):
            if (candidate / "environment").is_dir():
                return candidate
    return None


def describe(finding: dict) -> str:
    if finding["rule"] == "generator_outside_build_dir":
        where = "LEFT IN THE IMAGE" if finding["left_in_image"] else "deleted during the build"
        return f"generator not under _build/ ({where}): {finding['path']}"
    labels = {
        "answer_file": "file holding the expected result",
        "answer_file_written_at_build": "build step writes an expected-result file",
        "instruction_names_answer_file": "instruction points at an expected-result file",
        "build_dir_copied_elsewhere": "_build copied outside /tmp/_build",
        "build_script_run_outside_tmp": "_build script run outside /tmp/_build",
    }
    quoted = finding["rule"] in ("instruction_names_answer_file", "answer_file_written_at_build")
    detail = finding["message"].split("`")[1] + (f" (in {finding['path']})" if finding["rule"] == "answer_file_written_at_build" else "") if quoted else finding["path"]
    return f"{labels[finding['rule']]}: {detail}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    runs_dir = Path(sys.argv[1])
    runs = sys.argv[2:] or sorted(p.name for p in runs_dir.iterdir() if (p / "tasks").is_dir())
    checked = flagged = 0
    for run in runs:
        for task_dir in sorted((runs_dir / run / "tasks").iterdir()):
            task = final_version(task_dir) if task_dir.is_dir() else None
            if task is None:
                continue
            checked += 1
            findings = answer_key_findings(task)
            left = [f for f in findings if f["rule"] != "generator_outside_build_dir" or f["left_in_image"]]
            status = "FAIL" if left else ("ok  " if not findings else "ok* ")
            flagged += bool(left)
            print(f"{status} {run}/{task_dir.name} {task.parent.name} {task.name}")
            for finding in findings:
                print(f"       - {describe(finding)}")
    print(f"\n{checked} tasks checked, {flagged} ship an answer.")
    print("FAIL = an answer is visible to the solver.  ok* = the generator is deleted during the build, so nothing")
    print("is visible, but it is not under _build/ and would be sent back for repair under the new convention.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
