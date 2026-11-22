"""Batch pipeline: generate, gate, evaluate, audit, ship.

    python -m pipeline.run --run-id batch1 --n 12

Run the same command again to resume a killed run: every step that finished is
read back from disk instead of being redone. Two copies may run on the same
run ID at once; each task is worked on by whichever copy locks it first.
"""

import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from generator import author, config
from generator.llm import LLM
from generator.taxonomy import TASK_TYPES, sample_seed
from pipeline.analysis import analyse_task, classify_unsolved
from pipeline import flags, quality
from pipeline.candidate import build_candidate, read_task_files
from pipeline.evaluate import evaluate_band
from pipeline.harbor_runner import job_limiter
from pipeline.report import write_report
from pipeline.store import RunStore, atomic_write_json, atomic_write_text, file_lock, from_record_path, now_iso, read_json, to_record_path

# Subjects generated tasks must not resemble. Empty until a run needs one.
AVOID_SUBJECTS: list[str] = []


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=config.ROOT_DIR).stdout.strip()
    except OSError:
        return ""


def _git_commit() -> str:
    return _git("rev-parse", "--short", "HEAD")


def _uncommitted_changes() -> list[str]:
    """Tracked files that differ from the commit, so a run's code version is never ambiguous."""
    return [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no").splitlines() if line.strip()]


def existing_summaries(store: RunStore, exclude: str) -> list[str]:
    """One line per task already designed in this run, plus any subjects to avoid."""
    lines = list(AVOID_SUBJECTS)
    for spec_path in sorted(store.tasks_dir.glob("*/spec.json")):
        if spec_path.parent.name == exclude:
            continue
        spec = read_json(spec_path) or {}
        lines.append(f"{spec.get('title', '')}: {spec.get('description', '')}")
    return lines


class ReviewUnavailable(RuntimeError):
    """A review that a shipping decision depends on could not be obtained."""


def review_fairness(store: RunStore, task_id: str, task_path: Path, version: int, llm: LLM) -> dict:
    """Is every test's expectation stated in the instruction or discoverable in the container?

    Runs only on tasks that landed in the band. It labels the task fair or
    unfair and never sends it back for repair; an unfair task is not shipped.
    """
    tdir = store.task_dir(task_id)
    path = tdir / "reviews" / f"alignment-v{version}.json"
    cached = read_json(path)
    if cached:
        return cached

    instruction = (task_path / "instruction.md").read_text()
    tests_source = (task_path / "tests" / "test_outputs.py").read_text()
    environment = {p: c for p, c in read_task_files(task_path).items() if p.startswith("environment/")}
    names = [name for name, _ in quality.find_test_functions(tests_source)]
    try:
        review = author.review_alignment(llm, instruction, tests_source, environment, names, tdir / "llm")
    except author.AuthoringError as exc:
        # Fail closed. A review that could not be read has not shown the task
        # to be fair, so the task must not ship on it. Nothing is cached: the
        # task keeps no final state and the review is tried again on a re-run.
        store.log_event(task_id, "alignment", "unavailable", version=version, reason=str(exc)[:400])
        raise ReviewUnavailable(f"fairness review gave no usable answer: {exc}") from exc
    verdict = quality.judge_alignment(instruction, tests_source, environment, review)
    result = {"version": version, "fair": verdict.passed, "reason": verdict.reason, "review": review, **verdict.details}
    atomic_write_json(path, result)
    store.log_event(
        task_id,
        "alignment",
        "fair" if result["fair"] else "unfair",
        version=version,
        reason=result["reason"],
        problems=result["problems"],
        evidence=str(path.relative_to(store.run_dir)),
    )
    return result


EVIDENCE_DIR_NAME = "_evidence"


def _shipping_name(slug: str, run_id: str, task_id: str) -> str:
    """The folder name for a shipped task. Two different tasks never share one.

    Shipping the same task again (a resumed run) reuses its folder. A different
    task whose slug is already taken gets a numeric suffix.
    """
    evidence_root = config.OUTPUT_DIR / EVIDENCE_DIR_NAME
    name, n = slug, 1
    while True:
        existing = read_json(evidence_root / name / "verdict.json")
        if existing is None and not (config.OUTPUT_DIR / name).exists():
            return name
        if existing and (existing.get("run_id"), existing.get("task_id")) == (run_id, task_id):
            return name
        n += 1
        name = f"{slug}-{n}"


def ship(
    store: RunStore, task_id: str, state: dict, band: dict, analysis: dict | None, fairness: dict | None,
    confirm: dict | None, task_flags: list[dict] | None = None, extra: dict | None = None,
) -> Path:
    """Copy a learnable task to output/, with the evidence for its verdict beside it.

    ``output/<task>/`` holds the task's own files and nothing else, exactly the
    layout Harbor and the README describe. The evidence goes to
    ``output/_evidence/<task>/``: ``verdict.json`` (the pass count, the flags,
    every run's audit) and ``WHY_IT_FAILS.md`` (the same for a reader, with a
    banner when the task is flagged and so not verified).
    """
    task_flags = list(task_flags or [])
    source = from_record_path(state["task_path"])
    with file_lock(config.OUTPUT_DIR / ".ship.lock"):
        name = _shipping_name(source.name, store.run_dir.name, task_id)
        target = config.OUTPUT_DIR / name
        verdict = {
            "task": name,
            "run_id": store.run_dir.name,
            "task_id": task_id,
            "version": state["version"],
            "verdict": "learnable",
            "passes": band["passes"],
            "runs": band["valid_runs"],
            "evaluator": {
                "agent": config.EVAL_AGENT,
                "model": config.EVAL_MODEL,
                "reasoning_effort": config.EVAL_REASONING_EFFORT,
            },
            "generator_model": config.MODEL,
            "confirmation": ({"passes": confirm["passes"], "runs": confirm["valid_runs"], "verdict": confirm["verdict"]} if confirm else None),
            # Shipping is decided by the pass count above. Everything below is
            # the record of why runs failed and what a reader should look at.
            "flagged": bool(task_flags),
            "needs_human_read": bool(task_flags),
            "flags": task_flags,
            "why_it_fails": flags.failed_runs(analysis),
            "fairness": None if fairness is None else {
                "fair": fairness["fair"],
                "reason": fairness["reason"],
                "tests": [
                    {k: t.get(k) for k in ("test", "checks", "quote", "discoverable_from", "fair", "issue")} for t in fairness.get("tests", [])
                ],
            },
            "run_analysis": None if analysis is None else [
                {
                    **{k: run.get(k) for k in ("attempt", "outcome", "label", "explanation", "failed_requirement", "failed_tests")},
                    **({"audits": run["audits"], "votes": run["votes"]} if run.get("audits") else {}),
                }
                for run in analysis["runs"]
            ],
            **(extra or {}),
            "design": list(config.design()),
            "records": f"runs/{store.run_dir.name}/tasks/{task_id}",
            "shipped_at": now_iso(),
        }
        staging = config.OUTPUT_DIR / f".{name}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        evidence = config.OUTPUT_DIR / EVIDENCE_DIR_NAME / name
        atomic_write_json(evidence / "verdict.json", verdict)
        atomic_write_text(evidence / "WHY_IT_FAILS.md", flags.why_it_fails_markdown(name, band["passes"], band["valid_runs"], analysis, task_flags))
    return target


def process_task(store: RunStore, index: int, llm: LLM, *, author_only: bool = False) -> dict:
    """Generate task ``index`` of the run and take it as far as it gets."""
    task_id = f"t{index:03d}"
    # Two runs given the same seed key draw the same seeds, so they can be compared task for task.
    run = read_json(store.run_dir / "run.json") or {}
    seed_key = run.get("seed_key") or store.run_dir.name
    seed = sample_seed(seed_key, index, task_types=tuple(run.get("task_types") or ()))
    return process_candidate(store, task_id, seed, llm, author_only=author_only)


def process_candidate(
    store: RunStore, task_id: str, seed, llm: LLM, *, author_only: bool = False,
) -> dict:
    """Take one candidate from wherever it stands to its final state.

    A task is learnable when 1 to 3 of its five valid runs pass; that one
    measurement is the verdict.
    """
    tdir = store.task_dir(task_id)
    with file_lock(tdir / ".lock", blocking=False) as held:
        if not held:
            return {"task_id": task_id, "final": "skipped (another process is working on it)"}

        state_path = tdir / "state.json"
        state = read_json(state_path) or {}
        if state.get("final"):
            return state

        def finish(final: str, **extra) -> dict:
            state = read_json(state_path) or {"task_id": task_id}
            state.update(final=final, finished_at=now_iso(), **extra)
            atomic_write_json(state_path, state)
            logged = {k: v for k, v in extra.items() if k not in ("task_path", "flags")}
            if "flags" in extra:
                logged["flags"] = len(extra["flags"])  # the flags themselves are in state.json and the evidence folder
            store.log_event(task_id, "final", final, **logged)
            return state

        try:
            state = build_candidate(store, task_id, seed, llm, existing_summaries(store, task_id), run_gates=not author_only)
            if author_only and state["status"] == "authored":
                # Written and stored, nothing run. No final state: running the
                # batch again without --author-only takes it through the gates.
                return {**state, "final": None, "error": "authored (not yet checked or measured)"}
            if state["status"] != "ready":
                return finish(f"rejected:{state.get('rejected_at')}", reason=state.get("reason"))

            task_path, version = from_record_path(state["task_path"]), state["version"]
            band = evaluate_band(store, task_id, task_path, version)
            if band["verdict"] == "too_hard":
                # Nobody solved it. Audit every run to tell a hard task from a broken one.
                # A 0/5 task does not ship either way. "broken" or "too_hard" is
                # an annotation for a reader, made from the same two judges.
                audit = analyse_task(store, task_id, task_path, version, llm)
                kind, detail = classify_unsolved(audit)
                # The run audits take the tests' word for what was required: in
                # b8a they blamed the agent ten times out of ten for two unfair
                # tasks. The fairness review checks each test against the
                # instruction and the container, so it is asked here as well.
                try:
                    fairness = review_fairness(store, task_id, task_path, version, llm)
                except ReviewUnavailable as exc:
                    fairness = {"fair": True, "problems": []}
                    detail += f"; no fairness review ({str(exc)[:120]})"
                if not fairness["fair"]:
                    kind = "broken"
                    detail += "; fairness review: " + "; ".join(fairness["problems"])[:400]
                return finish(
                    f"rejected:{kind}",
                    passes=0,
                    reason=detail,
                    labels=audit["labels"],
                    same_tests_failed=audit["tests_failed_in_every_failed_run"],
                )
            if band["verdict"] != "learnable":
                return finish(f"rejected:{band['verdict']}", passes=band["passes"], reason=f"{band['passes']}/{band['valid_runs']} passed")

            # In the band: the task ships. That is the definition of
            # learnable and the whole of the decision. One thing can still
            # stop it, because a program decides it without judgement: a pass
            # whose commands touched the grading files.
            tampered = flags.tampered_passes(tdir, version)
            if tampered:
                return finish(
                    "rejected:tampered_pass",
                    passes=band["passes"],
                    reason=f"attempt {tampered[0]['attempt']} passed after a command that {tampered[0]['meaning']}: {tampered[0]['command']}"[:600],
                )
            # The run audit and the fairness review explain why runs failed and
            # raise flags for a person to read. They reject nothing, and a judge
            # that gives no usable answer leaves a flag, not a stuck task.
            analysis, fairness, no_audit, no_review = None, None, "", ""
            try:
                analysis = analyse_task(store, task_id, task_path, version, llm, revote=True)
            except author.AuthoringError as exc:
                no_audit = str(exc)
                store.log_event(task_id, "analysis", "unavailable", version=version, reason=no_audit[:400])
            try:
                fairness = review_fairness(store, task_id, task_path, version, llm)
            except ReviewUnavailable as exc:
                no_review = str(exc)
            task_flags = flags.collect(analysis, fairness, no_audit=no_audit, no_review=no_review)

            shipped = ship(store, task_id, state, band, analysis, fairness, None, task_flags)
            return finish(
                "shipped",
                passes=band["passes"],
                flagged=bool(task_flags),
                flags=task_flags,
                output=to_record_path(shipped),
            )
        except Exception as exc:  # leave the task resumable and keep the batch going
            store.log_event(task_id, "error", type(exc).__name__, reason=str(exc)[:500])
            return {"task_id": task_id, "final": None, "error": f"{type(exc).__name__}: {exc}"}


def start_run(store: RunStore, *, note: str, dirty: list[str], extra: dict | None = None) -> None:
    """Write run.json once, recording exactly which code and settings the run used."""
    run_path = store.run_dir / "run.json"
    with file_lock(store.run_dir / ".run.lock"):
        if not run_path.exists():
            atomic_write_json(
                run_path,
                {
                    "run_id": store.run_dir.name,
                    "started_at": now_iso(),
                    "code_commit": _git_commit(),
                    "uncommitted_changes": dirty,
                    "change_under_test": note,
                    "generator_model": config.MODEL,
                    "eval_agent": config.EVAL_AGENT,
                    "eval_model": config.EVAL_MODEL,
                    "eval_reasoning_effort": config.EVAL_REASONING_EFFORT,
                    "n_runs": config.N_RUNS,
                    "band": [config.BAND_LOW, config.BAND_HIGH],
                    **(extra or {}),
                },
            )


def refuse_dirty_tree(allow: bool) -> list[str] | None:
    """Return the uncommitted files, or None (after printing why) if the run must not start."""
    dirty = _uncommitted_changes()
    if dirty and not allow:
        print(
            "Uncommitted changes to tracked files:\n  " + "\n  ".join(dirty[:10])
            + "\nCommit them first so the run records exactly which code it used, or pass --allow-dirty.",
            file=sys.stderr,
        )
        return None
    return dirty


def attach_retry_log(llm: LLM, store: RunStore) -> None:
    """Put every retried gateway call in the ledger, marked rate_limited or error."""

    def log_retry(*, purpose, attempt, rate_limited, error, wait_sec, log_dir):
        # log_dir is runs/<run>/tasks/<task_id>/llm
        task_id = Path(log_dir).parent.name if log_dir else None
        store.log_event(
            task_id, "llm_retry", "rate_limited" if rate_limited else "error",
            purpose=purpose, attempt=attempt, wait_sec=wait_sec, reason=error,
        )

    llm.on_retry = log_retry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True, help="run directory under runs/; reuse it to resume")
    parser.add_argument("--n", type=int, default=10, help="number of candidate tasks (default 10)")
    parser.add_argument("--start", type=int, default=0, help="first seed index (default 0)")
    parser.add_argument(
        "--workers", type=int, default=config.DEFAULT_WORKERS,
        help=f"tasks worked on at once (default {config.DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--harbor-jobs", type=int, default=config.MAX_HARBOR_JOBS,
        help=f"Harbor jobs (oracle, nop, band attempts) running at once (default {config.MAX_HARBOR_JOBS})",
    )
    parser.add_argument(
        "--author-only", action="store_true",
        help="only write the tasks (model calls, no Docker): no gates, no agent runs. Run the same --run-id again "
        "without this flag to check and measure them",
    )
    parser.add_argument("--note", default="", help="what this run is testing (the one change since the last run)")
    parser.add_argument(
        "--design", default=",".join(config.DEFAULT_DESIGN) or "none",
        help=f"design features for a new run, comma-separated, from {config.DESIGN_FEATURES}; 'none' for the b7 design "
        f"(default {','.join(config.DEFAULT_DESIGN) or 'none'}). A resumed run keeps the design it started with",
    )
    parser.add_argument("--seed-key", default="", help="draw seeds as if the run had this id (default: the run id), so two runs get the same seeds")
    parser.add_argument(
        "--task-types", default="",
        help=f"only draw seeds of these task types, comma-separated, from {sorted(TASK_TYPES)} "
        "(default: all of them, balanced). A resumed run keeps the types it started with",
    )
    parser.add_argument("--allow-dirty", action="store_true", help="run even with uncommitted changes to tracked files")
    args = parser.parse_args()
    task_types = tuple(t for t in args.task_types.split(",") if t)
    unknown = [t for t in task_types if t not in TASK_TYPES]
    if unknown:
        parser.error(f"unknown task type(s) {unknown}; choose from {sorted(TASK_TYPES)}")

    dirty = refuse_dirty_tree(args.allow_dirty)
    if dirty is None:
        return 2

    store = RunStore(config.RUNS_DIR / args.run_id)
    existing = read_json(store.run_dir / "run.json")
    if existing:
        # A resumed run is built the way it was started. Runs from before designs were recorded used none.
        features = tuple(existing.get("design") or ())
    else:
        features = tuple(f for f in args.design.split(",") if f and f != "none")
    config.set_design(features)
    start_run(
        store, note=args.note, dirty=dirty,
        extra={"design": list(config.design()), "seed_key": args.seed_key or args.run_id, "task_types": list(task_types)},
    )
    job_limiter.set_limit(args.harbor_jobs)
    store.log_event(
        None, "run", "start",
        n=args.n, start=args.start, workers=args.workers, harbor_jobs=args.harbor_jobs, code_commit=_git_commit(),
        uncommitted_changes=dirty, note=args.note, design=list(config.design()),
    )

    llm = LLM()
    attach_retry_log(llm, store)
    indices = range(args.start, args.start + args.n)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for state in pool.map(lambda i: process_task(store, i, llm, author_only=args.author_only), indices):
            print(f"{state.get('task_id')}: {state.get('final') or state.get('error')}", flush=True)

    store.log_event(
        None, "run", "end",
        harbor_jobs_total=job_limiter.total, harbor_jobs_peak=job_limiter.peak, harbor_jobs_limit=job_limiter.limit,
    )
    report = write_report(store)
    print(f"\nReport: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
