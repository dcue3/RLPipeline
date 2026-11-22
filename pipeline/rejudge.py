"""Apply the shipping rule to runs that are already measured, without running any agent.

    python -m pipeline.rejudge <run_id>          # every measured task of the run
    python -m pipeline.rejudge <run_id>/t005     # one task

The rule: a task ships when 1 to 3 of its five valid runs
pass. The run audit and the fairness review no longer reject anything; what
they found becomes flags on the shipped task (see ``pipeline/flags.py``).

For each task of the run whose measurement landed in the band:

- **Rejected as unfair** (``rejected:unfair_runs``, ``rejected:unfair_tests``):
  it is shipped with its flags, unless something a program decides stops it.
  Today's structural gate is run on its files (older tasks were written before
  some of its checks existed), and its passing runs are scanned for commands
  that touched the grading files. A task that fails either gets that verdict
  instead.
- **Already shipped:** its evidence folder is rewritten with its flags and the
  "why it fails" page. Its verdict does not change.

Nothing is deleted. The verdict that was acted on stays in ``state.json`` under
``previous_verdicts`` with the reason for the change, the fairness verdict it
rested on is kept as ``alignment-vN.superseded-K.json``, and each change is
written to the run's ledger as a ``rejudge`` event.
"""

import sys
from pathlib import Path

from generator import config
from generator.taxonomy import STACKS
from pipeline import flags, quality
from pipeline.candidate import read_task_files
from pipeline.gates import structural_gate
from pipeline.store import RunStore, atomic_write_json, file_lock, from_record_path, now_iso, read_json

# Verdicts the rule can change: the old unfair ones, and a tampered pass, which is
# judged again because the command scan's rule for it was narrowed to writes.
UNFAIR = ("rejected:unfair_runs", "rejected:unfair_tests", "rejected:tampered_pass")
WHY = (
    "Shipping is decided by the pass count alone (1 to 3 of 5 valid runs), which is the definition. "
    "The run audit and the fairness review are recorded as flags and no longer reject a task."
)


def _fairness_now(tdir: Path, version: int, files: dict[str, str], note: dict) -> dict | None:
    """The stored fairness review, judged by the current rule. The verdict it replaces is kept."""
    path = tdir / "reviews" / f"alignment-v{version}.json"
    stored = read_json(path)
    if not stored or not isinstance(stored.get("review"), dict):
        return None
    environment = {p: c for p, c in files.items() if p.startswith("environment/")}
    verdict = quality.judge_alignment(files.get("instruction.md", ""), files.get("tests/test_outputs.py", ""), environment, stored["review"])
    result = {"version": version, "fair": verdict.passed, "reason": verdict.reason, "review": stored["review"], **verdict.details}
    if (stored.get("fair"), stored.get("problems")) != (result["fair"], result["problems"]):
        n = 1
        while (tdir / "reviews" / f"alignment-v{version}.superseded-{n}.json").exists():
            n += 1
        atomic_write_json(tdir / "reviews" / f"alignment-v{version}.superseded-{n}.json", stored)
        atomic_write_json(path, {**result, "rejudged": note})
    return result


def rejudge(store: RunStore, task_id: str, code_commit: str = "") -> dict:
    """Apply the shipping rule to one stored task. Returns what happened."""
    from pipeline.run import ship  # run imports this module's siblings; keep the import local

    tdir = store.task_dir(task_id)
    with file_lock(tdir / ".lock", blocking=False) as held:
        if not held:
            return {"changed": False, "reason": "another process is working on this task"}
        state_path = tdir / "state.json"
        state = read_json(state_path) or {}
        final = state.get("final")
        if final not in (*UNFAIR, "shipped"):
            return {"changed": False, "reason": f"final state is {final!r}: the rule changes nothing for it"}
        version = state.get("version")
        band = read_json(tdir / "band" / f"v{version}" / "summary.json") or {}
        if not band.get("complete") or not config.BAND_LOW <= band.get("passes", -1) <= config.BAND_HIGH:
            return {"changed": False, "reason": "its measurement is not in the band"}

        task_path = from_record_path(state["task_path"])
        seed = read_json(tdir / "seed.json") or {}
        note = {"at": now_iso(), "code_commit": code_commit, "why": WHY}
        previous = {"final": final, "reason": state.get("reason"), "finished_at": state.get("finished_at")}

        def conclude(new_final: str, **extra) -> dict:
            if new_final != final:
                state.setdefault("previous_verdicts", []).append({**previous, "changed_at": note["at"], "changed_because": WHY, "code_commit": code_commit})
            for key in ("reason",):
                state.pop(key, None)
            state.update(final=new_final, finished_at=state.get("finished_at") if new_final == final else note["at"], **extra)
            atomic_write_json(state_path, state)
            logged = {k: (len(v) if k == "flags" else v) for k, v in extra.items() if k != "output"}
            store.log_event(task_id, "rejudge", new_final, version=version, previous_final=final, code_commit=code_commit, **logged)
            return {"changed": new_final != final, "final": new_final, "flags": len(extra.get("flags") or []), "reason": extra.get("reason", "")}

        if final in UNFAIR:
            # What a program decides still decides. An older task may predate a structural check.
            base_image = STACKS.get(seed.get("stack"), {}).get("base_image")
            gate = structural_gate(task_path, base_image)
            if not gate.passed:
                return conclude("rejected:structural", reason=gate.failure_report[:600])
            tampered = flags.tampered_passes(tdir, version)
            if tampered:
                return conclude(
                    "rejected:tampered_pass",
                    reason=f"attempt {tampered[0]['attempt']} passed after a command that {tampered[0]['meaning']}: {tampered[0]['command']}"[:600],
                )

        files = read_task_files(task_path)
        analysis = read_json(tdir / "analysis" / f"v{version}.json")
        fairness = _fairness_now(tdir, version, files, note)
        task_flags = flags.collect(
            analysis, fairness,
            no_audit="no run audit is on record for this task", no_review="no fairness review is on record for this task",
        )
        confirm = read_json(tdir / "confirm" / f"v{version}" / "summary.json")
        old_evidence = read_json(from_record_path(state["output"]).parent / "_evidence" / from_record_path(state["output"]).name / "verdict.json") if state.get("output") else None
        extra = {
            "design": (read_json(store.run_dir / "run.json") or {}).get("design") or [],
            "rejudged": note,
            **({"previous_verdict": previous} if final != "shipped" else {}),
            **({"shipped_at": old_evidence["shipped_at"]} if old_evidence and old_evidence.get("shipped_at") else {}),
        }
        from pipeline.store import to_record_path

        shipped = ship(store, task_id, state, band, analysis, fairness, confirm if confirm and confirm.get("complete") else None, task_flags, extra)
        return conclude("shipped", passes=band["passes"], flagged=bool(task_flags), flags=task_flags, output=to_record_path(shipped))


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m pipeline.rejudge <run_id>  or  <run_id>/<task_id>")
    from pipeline.report import write_report
    from pipeline.run import _git_commit

    run_id, _, only = sys.argv[1].partition("/")
    run_dir = config.RUNS_DIR / run_id
    store = RunStore(run_dir)
    if not store.tasks_dir.is_dir():
        sys.exit(f"No such run: {Path('runs') / run_id}")
    task_ids = [only] if only else sorted(p.name for p in store.tasks_dir.iterdir() if (p / "state.json").exists())
    commit, changed = _git_commit(), 0
    for task_id in task_ids:
        if not (store.task_dir(task_id) / "state.json").exists():
            sys.exit(f"No such task: {Path('runs') / run_id / 'tasks' / task_id}")
        result = rejudge(store, task_id, commit)
        if result.get("final"):
            changed += result["changed"]
            was = "was rejected, now " if result["changed"] else ""
            print(f"{run_id}/{task_id}: {was}{result['final']}" + (f", {result['flags']} flag(s)" if result["final"] == "shipped" else f" ({result['reason'][:120]})"))
        elif only:
            print(f"{run_id}/{task_id}: unchanged ({result['reason']})")
    print(f"{changed} verdict(s) changed. Report: {write_report(store)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
