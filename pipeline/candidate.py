"""Build one candidate task, up to the point where it can be evaluated.

    seed -> pitch -> spec -> files -> structural -> oracle x2 + nop
                                          ^________ repair ________|
                                   (one repair per kind of failure)

A task is "ready" once the structural and functional gates pass. The spec
review and the requirement-coverage check are advisory: their findings are
recorded but cannot reject a task. Fairness is judged after the band
measurement (see pipeline/run.py), on tasks that turned out to be learnable.

Each step stores its output before the next begins and is skipped if that
output already exists, so a killed run resumes without repeating (or paying
again for) finished work.
"""

import shutil
from pathlib import Path

from generator import author, config
from generator.llm import LLM
from generator.public_seeds import novelty_gate
from generator.taxonomy import STACKS, SpecSeed
from pipeline import build_check, quality
from pipeline.gates import GateResult, InfrastructureError, functional_gate, structural_gate
from pipeline.store import RunStore, Timer, atomic_write_json, read_json, to_record_path

DONE_STATUSES = ("ready", "rejected")


def _llm_cost(task_dir: Path) -> float:
    return round(
        sum((read_json(p) or {}).get("cost_usd", 0.0) for p in (task_dir / "llm").glob("*.json")), 6
    )


def version_dir(task_dir: Path, version: int, slug: str) -> Path:
    return task_dir / f"v{version}" / slug


def read_task_files(path: Path) -> dict[str, str]:
    """The model-owned files of a task directory, as {relative path: content}."""
    files: dict[str, str] = {}
    for file in sorted(path.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(path).as_posix()
        if rel in ("task.toml", "tests/test.sh") or not author.MODEL_WRITABLE.match(rel):
            continue
        try:
            files[rel] = author.strip_code_owned(rel, file.read_text())
        except UnicodeDecodeError:
            continue
    return files


# --- Spec ------------------------------------------------------------------


def _reviewed_spec(store: RunStore, task_id: str, tdir: Path, seed: SpecSeed, llm: LLM, existing: list[str]) -> dict:
    """Write the spec, review it once, and revise it once if the review found problems.

    The review is advisory. It can prompt one revision and can never reject a
    task. The revised spec is not reviewed again: a second review could only
    produce a log line, and costs over a minute per task. Whether the task is
    any good is decided by the oracle, the band measurement and the audits.
    """
    llm_dir = tdir / "llm"
    review_path = tdir / "reviews" / "spec.json"
    record = read_json(review_path) or {"rounds": []}
    spec = read_json(tdir / "spec.json")

    if spec is None:
        if seed.topic:
            pitch = {"title": seed.topic, "summary": seed.topic, "what_is_hard": ""}
        else:
            pitches = author.write_pitches(llm, seed, existing, llm_dir)
            pitch, score = author.choose_pitch(pitches, existing)
            atomic_write_json(tdir / "pitches.json", {"pitches": pitches, "chosen": pitch, "similarity": score})
        spec = author.write_spec(llm, seed, pitch, llm_dir)
        atomic_write_json(tdir / "spec.json", spec)
        store.log_event(task_id, "spec", "ok", slug=spec["slug"], title=spec.get("title"))

    if record.get("done"):
        return spec

    if not record["rounds"]:
        review = author.review_spec(llm, seed, spec, llm_dir)
        verdict = quality.judge_spec_review(spec, len(seed.skills), review)
        record["rounds"].append({"round": 1, "review": review, "verdict": verdict.to_dict()})
        record["accepted"] = verdict.passed
        record["done"] = verdict.passed
        atomic_write_json(review_path, record)
        store.log_event(
            task_id,
            "spec_review",
            "pass" if verdict.passed else "revise",
            round=1,
            reason=verdict.reason,
            problems=verdict.details.get("problems"),
            evidence=str(review_path.relative_to(store.run_dir)),
        )
        if verdict.passed:
            return spec

    if config.MAX_SPEC_REVISIONS < 1:
        record["done"] = True
        record["revised"] = False
        atomic_write_json(review_path, record)
        return spec

    # The review found problems (possibly in an earlier, killed process): revise once.
    problems = record["rounds"][0]["verdict"]["failure_report"].splitlines()
    atomic_write_json(tdir / "spec.round1.json", spec)  # keep what was reviewed
    try:
        spec = author.revise_spec(llm, seed, spec, problems, llm_dir)
        atomic_write_json(tdir / "spec.json", spec)
        store.log_event(task_id, "spec_revise", "ok", round=1, problems=len(problems))
        record["revised"] = True
    except author.AuthoringError as exc:
        # An unusable revision is not a reason to lose the task: keep the spec we have.
        store.log_event(task_id, "spec_revise", "skipped", round=1, reason=str(exc)[:300])
        record["revised"] = False
    record["done"] = True
    atomic_write_json(review_path, record)
    return spec


# --- Gates -----------------------------------------------------------------


def _gate_chain(tdir: Path, task_path: Path, version: int, seed: SpecSeed, run_name: str, task_id: str, spec: dict | None = None):
    """Yield gate results cheapest first, stopping at the first failure.

    A specification that promises state generated at build time is held to it.
    """
    yield structural_gate(task_path, STACKS[seed.stack]["base_image"])
    if seed.card:
        # A task seeded from a public task must not repeat its wording (generator/public_seeds.py).
        yield novelty_gate(task_path, seed.card)
    yield functional_gate(task_path, tdir / "jobs", f"{run_name}-{task_id}-v{version}")


def _run_gates(store: RunStore, task_id: str, tdir: Path, task_path: Path, version: int, seed: SpecSeed, spec: dict) -> GateResult:
    """Run the gate chain for one version. The outcome is cached on disk."""
    gate_path = tdir / "gates" / f"v{version}.json"
    cached = read_json(gate_path)
    if cached:
        return GateResult(**cached["final"])

    results: list[GateResult] = []
    chain = _gate_chain(tdir, task_path, version, seed, store.run_dir.name, task_id, spec)
    try:
        for result in _timed(chain, store, task_id, version, gate_path):
            results.append(result)
            if not result.passed:
                break
    except InfrastructureError as exc:
        # Nothing is cached and no repair is used: the task is exactly where it
        # was, and running the batch again picks it up at this gate.
        store.log_event(task_id, "infrastructure", "fail", version=version, reason=str(exc)[:500])
        raise

    # Advisory notes, not gates: which spec requirements the tests name, and
    # whether this version's instruction gives anything away.
    tests_path = task_path / "tests" / "test_outputs.py"
    coverage = quality.coverage_check(spec, tests_path.read_text()).to_dict() if tests_path.exists() else None
    instruction_path = task_path / "instruction.md"
    giveaways = quality.instruction_giveaways(instruction_path.read_text(), spec) if instruction_path.exists() else None
    environment_files = {p: c for p, c in read_task_files(task_path).items() if p.startswith("environment/")}
    environment_notes = quality.environment_giveaways(environment_files, spec)
    atomic_write_json(
        gate_path,
        {
            "version": version,
            "gates": [r.to_dict() for r in results],
            "final": results[-1].to_dict(),
            "notes": {
                "coverage": coverage,
                "instruction_check": giveaways,
                "environment_check": environment_notes,
                "instruction_rewrite": {
                    k: v for k, v in (read_json(tdir / "reviews" / "instruction_check.json") or {}).items()
                    if k in ("before", "rewritten", "after")
                },
            },
        },
    )
    return results[-1]


def _timed(chain, store: RunStore, task_id: str, version: int, gate_path: Path):
    """Log one ledger event per gate, with how long it took."""
    while True:
        with Timer() as timer:
            try:
                result = next(chain)
            except StopIteration:
                return
        store.log_event(
            task_id,
            result.gate,
            "pass" if result.passed else "fail",
            version=version,
            reason=result.reason,
            duration_sec=timer.seconds,
            evidence=str(gate_path.relative_to(store.run_dir)),
        )
        yield result


def _checked_instruction(store: RunStore, task_id: str, tdir: Path, spec: dict, files: dict[str, str], llm: LLM) -> str:
    """Check the new instruction for give-aways and ask for one rewrite if any are found.

    No model is used for the check. If it trips, the instruction alone is
    re-asked once with the offending sentences quoted. Authoring continues
    either way; the before and after are recorded.
    """
    path = tdir / "reviews" / "instruction_check.json"
    record = read_json(path)
    if record:
        return record["final_instruction"]

    instruction = files["instruction.md"]
    before = quality.instruction_giveaways(instruction, spec)
    record = {"before": before, "rewritten": False, "after": None, "first_instruction": instruction}
    if before["tripped"]:
        try:
            tests = files.get("tests/test_outputs.py") or (
                "# Not written yet. The tests will be written from this instruction, so keep every\n"
                "# acceptance criterion it has: each path, command, format and exit code.\n"
            )
            instruction = author.rewrite_instruction(llm, instruction, tests, before["findings"], tdir / "llm")
            record["rewritten"] = True
            record["after"] = quality.instruction_giveaways(instruction, spec)
        except author.AuthoringError as exc:
            record["rewrite_error"] = str(exc)[:300]
    record["final_instruction"] = instruction
    atomic_write_json(path, record)
    after = record["after"] or before
    store.log_event(
        task_id,
        "instruction_check",
        "clean" if not before["tripped"] else ("rewritten_clean" if record["rewritten"] and not after["tripped"] else "giveaways_remain"),
        found=len(before["findings"]),
        remaining=len(after["findings"]),
        kinds=sorted({f["kind"] for f in before["findings"]}),
        evidence=str(path.relative_to(store.run_dir)),
    )
    return instruction


BUILD_FAILURE_REASON = "environment did not build or start"


def failure_kind(gate: str, reason: str) -> str:
    """Which repair allowance a failed gate draws on: structural, build or functional.

    The functional gate reports two different things. An image that does not
    build has not run the solution or the tests at all, so repairing it says
    nothing about them and must not use up their repair.
    """
    if gate == "functional" and reason.startswith(BUILD_FAILURE_REASON):
        return "build"
    return gate


def gate_failures(tdir: Path, up_to_version: int) -> dict[str, int]:
    """How many versions so far ended in a failure of each kind."""
    counts: dict[str, int] = {}
    for version in range(1, up_to_version + 1):
        record = read_json(tdir / "gates" / f"v{version}.json")
        if record and not record["final"]["passed"]:
            kind = failure_kind(record["final"]["gate"], record["final"].get("reason", ""))
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _failing_tests(final: dict) -> int | None:
    """How many tests did not pass in the first oracle run of a gate result, or None if it has no test report."""
    oracle = (final.get("details") or {}).get("oracle") or []
    tests = (oracle[0] or {}).get("tests") if oracle else None
    if not tests:
        return None
    return sum(1 for t in tests if t.get("status") != "passed")


def repair_made_progress(tdir: Path, version: int) -> bool:
    """True if this version fails fewer tests than the last version that got as far as running them."""
    now = _failing_tests((read_json(tdir / "gates" / f"v{version}.json") or {}).get("final") or {})
    if now is None:
        return False
    for earlier in range(version - 1, 0, -1):
        final = (read_json(tdir / "gates" / f"v{earlier}.json") or {}).get("final") or {}
        before = _failing_tests(final)
        if before is not None and failure_kind(final.get("gate", ""), final.get("reason", "")) == "functional":
            return now < before
    return False


def earlier_repairs_note(tdir: Path, version: int, slug: str) -> str:
    """For a second or later repair: what each earlier one changed and what failed before and after it.

    Each repair is a fresh request. Without this it cannot know that a change
    has already been tried, or whether the last one helped.
    """
    lines = []
    for earlier in range(1, version):
        before = (read_json(tdir / "gates" / f"v{earlier}.json") or {}).get("final") or {}
        old, new = read_task_files(version_dir(tdir, earlier, slug)), read_task_files(version_dir(tdir, earlier + 1, slug))
        changed = sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))
        oracle = (before.get("details") or {}).get("oracle") or []
        failed = [t["name"] for t in (oracle[0].get("tests") or []) if t.get("status") != "passed"] if oracle else []
        lines.append(
            f"- Repair {earlier} followed \"{before.get('gate')}: {before.get('reason')}\""
            + (f" ({len(failed)} test(s) failing: {', '.join(failed[:12])})" if failed else "")
            + f" and changed {', '.join(changed) or 'nothing'}."
        )
    if not lines:
        return ""
    return "\n\nRepairs already made to this task, oldest first. The failure above is what remains after them:\n" + "\n".join(lines) + "\nDo not repeat a change that did not help."


# --- Build -----------------------------------------------------------------


def _kept(store: RunStore, task_id: str, tdir: Path, name: str, write) -> dict[str, str]:
    """The files one authoring call wrote, stored as soon as the call returns.

    A task's files are assembled into v1/ only when all of them exist. Until
    this was added, a task killed between its environment call and that point
    paid for the environment and tests calls again on resume (found by
    scripts/robustness_test.py, run rb1). A stored reply is read back.
    """
    path = tdir / "parts" / f"{name}.json"
    stored = read_json(path)
    if stored and isinstance(stored.get("files"), dict):
        return dict(stored["files"])
    files = write()
    atomic_write_json(path, {"part": name, "files": files})
    store.log_event(task_id, "part", name, files=sorted(files), evidence=str(path.relative_to(store.run_dir)))
    return files


def build_candidate(
    store: RunStore,
    task_id: str,
    seed: SpecSeed,
    llm: LLM,
    existing_summaries: list[str],
    *,
    run_gates: bool = True,
) -> dict:
    """Author a task and take it through every gate that precedes evaluation.

    Returns the task's state dict. ``status`` is ``ready`` (structural and
    functional gates passed),
    ``authored`` (gates skipped on request) or ``rejected``.
    """
    tdir = store.task_dir(task_id)
    state_path = tdir / "state.json"
    state = read_json(state_path) or {"task_id": task_id, "status": "new", "version": 0}
    if state["status"] in DONE_STATUSES:
        return state  # already decided by an earlier process

    def save(**updates):
        state.update(updates)
        state["llm_cost_usd"] = _llm_cost(tdir)
        atomic_write_json(state_path, state)
        return state

    def reject(stage: str, reason: str, **extra):
        store.log_event(task_id, "reject", stage, reason=reason, **extra)
        return save(status="rejected", rejected_at=stage, reason=reason, **{k: v for k, v in extra.items() if k == "limit"})

    atomic_write_json(tdir / "seed.json", seed.to_dict())
    llm_dir = tdir / "llm"

    try:
        spec = _reviewed_spec(store, task_id, tdir, seed, llm, existing_summaries)
        slug = spec["slug"]
        save(slug=slug, title=spec.get("title"), summary=spec.get("description"))

        v1 = version_dir(tdir, 1, slug)
        if not (v1 / "task.toml").exists():
            files = _kept(store, task_id, tdir, "environment", lambda: author.write_environment(llm, seed, spec, llm_dir))
            if config.design_has("early-build"):
                # Find out now whether the image builds, before the tests and the instruction are written on it.
                files = build_check.early_build(store, task_id, tdir, spec, files, llm)
            files.update(_kept(store, task_id, tdir, "tests", lambda: author.write_tests(llm, spec, files, llm_dir)))
            files["instruction.md"] = _checked_instruction(store, task_id, tdir, spec, files, llm)
            if v1.exists():
                shutil.rmtree(v1)
            author.assemble_task(v1, spec, seed, files)
            store.log_event(task_id, "author", "ok", version=1, files=sorted(files))
        if state["version"] < 1:
            save(version=1, status="authored", task_path=to_record_path(v1))

        if not run_gates:
            return save(status="authored")

        version = state["version"]
        while True:
            task_path = version_dir(tdir, version, slug)
            result = _run_gates(store, task_id, tdir, task_path, version, seed, spec)
            if result.passed:
                return save(status="ready", version=version, task_path=to_record_path(task_path), reason="")

            # Each kind of failure has its own small repair allowance. Failures
            # are counted from the stored gate results, so a resumed task gets
            # exactly the repairs it had left and never an extra one.
            kind = failure_kind(result.gate, result.reason)
            limit_name = config.REPAIR_LIMITS[kind]
            failures = gate_failures(tdir, version).get(kind, 0)
            allowed = getattr(config, limit_name)
            if kind == "functional" and failures == allowed + 1 and config.MAX_PROGRESS_REPAIRS and repair_made_progress(tdir, version):
                # The repair did not finish the job but it moved: one more.
                allowed += config.MAX_PROGRESS_REPAIRS
                store.log_event(task_id, "repair", "extra", version=version, reason="the last repair reduced the number of failing tests")
            if failures > allowed:
                return reject(result.gate, result.reason, version=version, limit=limit_name, failures=failures)

            next_path = version_dir(tdir, version + 1, slug)
            if not (next_path / "task.toml").exists():
                files = read_task_files(task_path)
                report = result.failure_report
                if config.design_has("full-reports"):
                    report += earlier_repairs_note(tdir, version, slug)
                changed = author.write_repair(llm, spec, files, report, llm_dir)
                files.update(changed)
                if next_path.exists():
                    shutil.rmtree(next_path)
                author.assemble_task(next_path, spec, seed, files)
                store.log_event(
                    task_id, "repair", "ok", version=version + 1, after=f"{result.gate}: {result.reason}", changed=sorted(changed)
                )
            version += 1
            save(version=version, status="authored", task_path=to_record_path(next_path))

    except build_check.BuildNeverPassed as exc:
        return reject("build", str(exc))
    except author.AuthoringError as exc:
        return reject("author", str(exc))
