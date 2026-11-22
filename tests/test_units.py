"""Unit tests for the parts of the pipeline that need no model and no Docker."""

import json
import tomllib

import pytest

from generator import author
from generator.similarity import similarity
from generator.taxonomy import DOMAINS, STACKS, TASK_TYPES, sample_seed
from pipeline.store import RunStore, atomic_write_json, file_lock, read_json


@pytest.fixture(autouse=True)
def default_design():
    """Every test starts from the default design and cannot leak its own into the next."""
    from generator import config

    config.set_design(config.DEFAULT_DESIGN)
    yield
    config.set_design(config.DEFAULT_DESIGN)


def test_seed_is_deterministic():
    assert sample_seed("run-a", 3).to_dict() == sample_seed("run-a", 3).to_dict()
    assert sample_seed("run-a", 3).to_dict() != sample_seed("run-b", 3).to_dict()


def test_domains_do_not_repeat_within_a_pass():
    domains = [sample_seed("run-a", i).domain for i in range(len(DOMAINS))]
    assert sorted(domains) == sorted(DOMAINS)


def test_seed_choices_are_compatible():
    for index in range(200):
        seed = sample_seed("compat", index)
        info = DOMAINS[seed.domain]
        assert seed.stack in info["stacks"] and seed.stack in STACKS
        assert seed.task_type in info["task_types"] and seed.task_type in TASK_TYPES
        assert 2 <= len(seed.skills) <= 3 and len(set(seed.skills)) == len(seed.skills)


def test_taxonomy_references_are_valid():
    for name, info in DOMAINS.items():
        assert set(info["stacks"]) <= set(STACKS), name
        assert set(info["task_types"]) <= set(TASK_TYPES), name
        assert set(info["neighbours"]) <= set(DOMAINS), name
        assert len(info["skills"]) >= 8, name


def test_parse_file_blocks_and_path_rules():
    text = "<<<FILE environment/Dockerfile>>>\nFROM x\n<<<END>>>\n\n<<<FILE solution/solve.sh>>>\necho hi\n<<<END>>>"
    files = author.parse_file_blocks(text)
    assert files == {"environment/Dockerfile": "FROM x\n", "solution/solve.sh": "echo hi\n"}
    for bad in ("task.toml", "tests/test.sh", "../x", "environment/../../etc/passwd", "/etc/passwd"):
        with pytest.raises(author.AuthoringError):
            author.parse_file_blocks(f"<<<FILE {bad}>>>\nx\n<<<END>>>")
    with pytest.raises(author.AuthoringError):
        author.parse_file_blocks("no blocks here")


def test_assembled_task_is_structurally_valid(tmp_path):
    from pipeline.gates import structural_gate

    seed = sample_seed("assemble", 0)
    spec = {"slug": "demo-task", "description": 'Checks "quotes" and\nnewlines.', "tags": ["A b"], "difficulty": "hard"}
    files = {
        "environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\nset -euo pipefail\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    pass\ndef test_b():\n    pass\ndef test_c():\n    pass\n",
        "instruction.md": "Do the thing.\n",
    }
    author.assemble_task(tmp_path / "demo-task", spec, seed, files)
    config = tomllib.loads((tmp_path / "demo-task" / "task.toml").read_text())
    assert config["task"]["name"] == "rlpipeline/demo-task"
    assert config["agent"]["timeout_sec"] >= 3600
    assert "canary" in (tmp_path / "demo-task/solution/solve.sh").read_text().splitlines()[1]
    assert "canary" not in (tmp_path / "demo-task/instruction.md").read_text()
    result = structural_gate(tmp_path / "demo-task", STACKS[seed.stack]["base_image"])
    assert result.passed, result.failure_report


def test_similarity_orders_sensibly():
    a = "Repair a log multiplexer whose pipeline loses exit codes"
    assert similarity(a, a) == 1.0
    assert similarity(a, "Fix the log multiplexer pipeline that loses exit codes") > similarity(
        a, "Convert timestamps across daylight saving changes"
    )


def test_store_ledger_and_atomic_write(tmp_path):
    store = RunStore(tmp_path / "run")
    store.log_event("t000", "spec", "ok", slug="x")
    store.log_event("t000", "structural", "pass")
    assert [e["stage"] for e in store.events()] == ["spec", "structural"]
    path = tmp_path / "run" / "state.json"
    atomic_write_json(path, {"a": 1})
    assert read_json(path) == {"a": 1} and json.loads(path.read_text()) == {"a": 1}
    assert not list(path.parent.glob("*.tmp"))


def test_file_lock_is_exclusive(tmp_path):
    lock = tmp_path / "x.lock"
    with file_lock(lock, blocking=False) as first:
        assert first
        with file_lock(lock, blocking=False) as second:
            assert not second
    with file_lock(lock, blocking=False) as again:
        assert again


# --- Quality checks ---------------------------------------------------------

from pipeline import quality  # noqa: E402

SPEC = {"requirements": [{"id": "R1", "text": "a"}, {"id": "R2", "text": "b"}]}
TESTS = '''
class TestThing:
    def test_one(self):
        """R1: exits non-zero on bad input."""
    def test_two(self):
        """R1, R2: writes the report."""
'''


def test_coverage_passes_when_every_requirement_and_test_is_linked():
    assert quality.coverage_check(SPEC, TESTS).passed


def test_coverage_flags_uncovered_unlabelled_and_unknown():
    result = quality.coverage_check(SPEC, 'def test_a():\n    """R1: x."""\ndef test_b():\n    """no id"""\ndef test_c():\n    """R9: y."""\n')
    assert not result.passed
    assert result.details["uncovered"] == ["R2"] and result.details["unlabelled"] == ["test_b"]
    assert "R9" in result.failure_report


def test_spec_review_verdict_is_computed_from_evidence():
    good = {
        "skills": [{"skill": "s1", "requirement_ids": ["R1"], "needed": True}, {"skill": "s2", "requirement_ids": ["R2"], "needed": True}],
        "requirements": [{"id": "R1", "behavioural": True, "testable": True, "deterministic": True}],
        "pitfalls": [{"index": 1, "knowable": True}],
        "gives_away_solution": False,
        "realistic": True,
    }
    assert quality.judge_spec_review(SPEC, 2, good).passed
    # A skill tied to a requirement that does not exist does not count.
    fake = dict(good, skills=[{"skill": "s1", "requirement_ids": ["R7"], "needed": True}, {"skill": "s2", "requirement_ids": [], "needed": True}])
    assert not quality.judge_spec_review(SPEC, 2, fake).passed
    for change in (
        {"requirements": [{"id": "R1", "behavioural": False, "testable": True, "deterministic": True, "issue": "names pipefail"}]},
        {"pitfalls": [{"index": 1, "knowable": False, "why": "hidden"}]},
        {"gives_away_solution": True},
        {"realistic": False},
        {"contradictions": [{"ids": ["R1", "R2"], "problem": "clash"}]},
    ):
        assert not quality.judge_spec_review(SPEC, 2, dict(good, **change)).passed, change


def test_alignment_accepts_instruction_quote_or_container_path():
    instruction = "The tool must exit with **status 3** when the input file is empty.\nWrite the report to `/app/out.json`."
    tests = 'def test_empty():\n    """x"""\ndef test_format():\n    """y"""\n'
    environment = {"environment/Dockerfile": "FROM x\nCOPY docs/ /app/docs/\n", "environment/docs/FORMAT.md": "fields: a, b"}

    def review(**first):
        entry = {"name": "test_empty", "checks": "exit code", "quote": "", "discoverable_from": "", "fair": True}
        entry.update(first)
        return {"tests": [entry, {"name": "test_format", "checks": "field names", "discoverable_from": "/app/docs/FORMAT.md", "fair": True}]}

    judge = lambda r: quality.judge_alignment(instruction, tests, environment, r)  # noqa: E731
    assert judge(review(quote="must exit with status 3 when the input file is empty")).passed
    # Discoverable in the container is as good as stated in the instruction.
    assert judge(review(discoverable_from="/app/docs/FORMAT.md")).passed
    # A test the reviewer calls fair is fair, whether or not what it cites can be found
    # (the citation requirement was later dropped).
    assert judge(review(quote="must print a usage message")).passed
    assert judge(review(discoverable_from="/srv/nowhere/secret.txt")).passed
    assert not judge(review(fair=False, issue="exact wording")).passed
    assert not judge(review(quote="must exit with status 3 when the input file is empty", inspects_implementation=True)).passed


def test_dockerfile_footer_is_added_once(tmp_path):
    seed = sample_seed("footer", 0)
    spec = {"slug": "demo", "description": "d"}
    files = {
        "environment/Dockerfile": "FROM debian:bookworm-slim\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    pass\n",
        "instruction.md": "x\n",
    }
    author.assemble_task(tmp_path / "v1", spec, seed, files)
    from pipeline.candidate import read_task_files

    seen_by_model = read_task_files(tmp_path / "v1")
    assert author.DOCKERFILE_FOOTER_MARKER not in seen_by_model["environment/Dockerfile"]
    author.assemble_task(tmp_path / "v2", spec, seed, seen_by_model)
    text = (tmp_path / "v2/environment/Dockerfile").read_text()
    assert text.count(author.DOCKERFILE_FOOTER_MARKER) == 1 and text.count("terminal-bench-canary") == 1


# --- Band evaluation rules ---------------------------------------------------

from pipeline.evaluate import band_verdict, classify_attempt  # noqa: E402
from pipeline.harbor_runner import TrialResult  # noqa: E402


def _trial(reward, exception=None, report=True, failed=0):
    tests = [{"name": f"t{i}", "status": "failed", "message": ""} for i in range(failed)]
    return TrialResult("x", "/x", reward, exception, None, 0.0, 0, 0, 1, 1.0, tests, report, "")


def test_attempt_classification():
    assert classify_attempt(_trial(1.0))[0] == "pass"
    assert classify_attempt(_trial(0.0, failed=2))[0] == "fail"
    # A run that passes after using all its time is still a pass.
    assert classify_attempt(_trial(1.0, "AgentTimeoutError"))[0] == "pass"
    # Timeouts, gateway errors and runs with no result are not model failures.
    assert classify_attempt(_trial(0.0, "AgentTimeoutError", failed=1))[0] == "invalid"
    assert classify_attempt(_trial(0.0, "BadRequestError", failed=1))[0] == "invalid"
    assert classify_attempt(_trial(None, "RuntimeError", report=False))[0] == "invalid"
    assert classify_attempt(_trial(0.0, report=False))[0] == "invalid"
    assert classify_attempt(None)[0] == "invalid"


def test_band_verdict_edges():
    assert [band_verdict(n) for n in range(6)] == ["too_hard", "learnable", "learnable", "learnable", "too_easy", "too_easy"]


def test_task_types_are_balanced_across_a_batch():
    from collections import Counter

    counts = Counter(sample_seed("balance", i).task_type for i in range(10))
    assert max(counts.values()) <= 3 and len(counts) >= 4, counts


def test_repair_cannot_drop_tests_or_assertions():
    old = "def test_a():\n    assert 1\n    assert 2\ndef test_b():\n    assert 3\n    assert 4\n    assert 5\n"
    assert author.weakened_tests(old, old) == ""
    assert "removed" in author.weakened_tests(old, "def test_a():\n    assert 1\n    assert 2\n    assert 3\n    assert 4\n")
    assert "assertions" in author.weakened_tests(old, "def test_a():\n    assert 1\ndef test_b():\n    assert 3\n")
    # Adding tests, or fixing a value without removing checks, is fine.
    assert author.weakened_tests(old, old + "def test_c():\n    assert 6\n") == ""


def test_truncated_file_block_is_an_error():
    with pytest.raises(author.AuthoringError):
        author.parse_file_blocks("<<<FILE instruction.md>>>\nok\n<<<END>>>\n<<<FILE tests/test_outputs.py>>>\ndef test_a(): pass\n")


def test_structural_gate_rejects_solution_references_and_wrong_workdir(tmp_path):
    from pipeline.gates import structural_gate

    seed = sample_seed("structural", 0)
    base = STACKS[seed.stack]["base_image"]
    good = {
        "environment/Dockerfile": f"FROM {base}\nWORKDIR /tmp/gen\nRUN true\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    pass\ndef test_b():\n    pass\ndef test_c():\n    pass\n",
        "instruction.md": "Do it.\n",
    }
    spec = {"slug": "demo", "description": "d"}
    author.assemble_task(tmp_path / "ok", spec, seed, good)
    assert structural_gate(tmp_path / "ok", base).passed
    assert (tmp_path / "ok/tests/conftest.py").exists()

    bad_workdir = dict(good, **{"environment/Dockerfile": f"FROM {base}\nWORKDIR /app\nWORKDIR /opt/x\n"})
    author.assemble_task(tmp_path / "wd", spec, seed, bad_workdir)
    assert "WORKDIR" in structural_gate(tmp_path / "wd", base).failure_report

    uses_solution = dict(good, **{"tests/test_outputs.py": good["tests/test_outputs.py"] + "X = '/app/solution/solve.sh'\n"})
    author.assemble_task(tmp_path / "sol", spec, seed, uses_solution)
    assert "reference solution" in structural_gate(tmp_path / "sol", base).failure_report


# --- Pre-band gates are lean: structural and functional only ----------------


def _minimal_task(tmp_path, seed, **overrides):
    files = {
        "environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": 'def test_only():\n    """No requirement id here."""\n',
        "instruction.md": "Run pytest in the repo and make it pass.\n",
    }
    files.update(overrides)
    author.assemble_task(tmp_path, {"slug": "demo", "description": "d"}, seed, files)
    return tmp_path


def test_structural_gate_allows_one_test_and_the_word_pytest(tmp_path):
    from pipeline.gates import structural_gate

    seed = sample_seed("lean", 0)
    task = _minimal_task(tmp_path / "ok", seed)
    assert structural_gate(task, STACKS[seed.stack]["base_image"]).passed


def test_structural_gate_limits_instruction_length(tmp_path):
    from generator import config
    from pipeline.gates import structural_gate

    seed = sample_seed("lean", 0)
    long_text = " ".join(["word"] * (config.INSTRUCTION_MAX_WORDS + 1)) + "\n"
    task = _minimal_task(tmp_path / "long", seed, **{"instruction.md": long_text})
    assert "words" in structural_gate(task, STACKS[seed.stack]["base_image"]).failure_report


def test_spec_size_limits_are_enforced():
    def spec(n_req, n_pit):
        return __import__("json").dumps({
            "slug": "a-b-c", "title": "t", "description": "d", "scenario": "s", "environment": [{"path": "/app/x"}],
            "solution_outline": ["do"],
            "requirements": [{"id": f"R{i}", "text": "x"} for i in range(n_req)],
            "pitfalls": [{"what": "w", "knowable_from": "running `make` in /app shows the mismatch"} for _ in range(n_pit)],
        })

    assert author._parse_spec(spec(5, 2))["slug"] == "a-b-c"
    for n_req, n_pit in ((7, 2), (3, 2), (5, 4), (5, 1)):
        with pytest.raises(author.AuthoringError):
            author._parse_spec(spec(n_req, n_pit))


def test_gate_chain_is_structural_then_functional_with_coverage_as_a_note(tmp_path, monkeypatch):
    from pipeline import candidate
    from pipeline.gates import GateResult

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    task = _minimal_task(tdir / "v1" / "demo", seed)
    monkeypatch.setattr(candidate, "functional_gate", lambda *a, **k: GateResult("functional", True, "ok"))
    spec = {"requirements": [{"id": "R1", "text": "x"}]}

    result = candidate._run_gates(store, "t000", tdir, task, 1, seed, spec)
    record = read_json(tdir / "gates" / "v1.json")
    # The test names no requirement id: coverage would have failed as a gate.
    assert result.passed and [g["gate"] for g in record["gates"]] == ["structural", "functional"]
    assert record["notes"]["coverage"]["passed"] is False
    assert [e["stage"] for e in store.events()] == ["structural", "functional"]


def _spec_review_setup(tmp_path, monkeypatch, review):
    from pipeline import candidate

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    atomic_write_json(tdir / "spec.json", {"slug": "demo", "title": "t", "requirements": [{"id": "R1", "text": "x"}]})
    calls = {"review": 0, "revise": 0}

    def fake_review(*a, **k):
        calls["review"] += 1
        return review

    def fake_revise(llm, seed, spec, problems, log_dir):
        calls["revise"] += 1
        return dict(spec, revised=True)

    monkeypatch.setattr(candidate.author, "review_spec", fake_review)
    monkeypatch.setattr(candidate.author, "revise_spec", fake_revise)
    return candidate, store, tdir, seed, calls


BAD_SPEC_REVIEW = {"skills": [], "requirements": [{"id": "R1", "behavioural": False, "issue": "names a flag"}], "pitfalls": []}
GOOD_SPEC_REVIEW = {"skills": [], "requirements": [{"id": "R1", "behavioural": True}], "pitfalls": []}


def test_spec_is_reviewed_once_and_revised_without_a_second_review(tmp_path, monkeypatch):
    candidate, store, tdir, seed, calls = _spec_review_setup(tmp_path, monkeypatch, BAD_SPEC_REVIEW)

    out = candidate._reviewed_spec(store, "t000", tdir, seed, None, [])
    assert out["revised"] is True and calls == {"review": 1, "revise": 1}
    # One review event only; nothing is logged for a review that did not run.
    assert [(e["stage"], e["outcome"]) for e in store.events()] == [("spec_review", "revise"), ("spec_revise", "ok")]
    assert len(read_json(tdir / "reviews" / "spec.json")["rounds"]) == 1

    # Resuming neither reviews nor revises again.
    candidate._reviewed_spec(store, "t000", tdir, seed, None, [])
    assert calls == {"review": 1, "revise": 1} and len(store.events()) == 2


def test_spec_that_passes_review_is_not_revised(tmp_path, monkeypatch):
    candidate, store, tdir, seed, calls = _spec_review_setup(tmp_path, monkeypatch, GOOD_SPEC_REVIEW)
    seed.skills = []
    out = candidate._reviewed_spec(store, "t000", tdir, seed, None, [])
    assert "revised" not in out and calls == {"review": 1, "revise": 0}
    assert [(e["stage"], e["outcome"]) for e in store.events()] == [("spec_review", "pass")]


def test_spec_revision_resumes_after_a_kill_without_reviewing_again(tmp_path, monkeypatch):
    candidate, store, tdir, seed, calls = _spec_review_setup(tmp_path, monkeypatch, BAD_SPEC_REVIEW)
    monkeypatch.setattr(candidate.author, "revise_spec", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        candidate._reviewed_spec(store, "t000", tdir, seed, None, [])  # killed during the revision

    monkeypatch.setattr(candidate.author, "revise_spec", lambda llm, seed, spec, problems, log_dir: dict(spec, revised=True))
    out = candidate._reviewed_spec(store, "t000", tdir, seed, None, [])
    assert out["revised"] is True and calls["review"] == 1


# --- Repair limits ------------------------------------------------------------


def _repair_setup(tmp_path, monkeypatch, outcomes):
    """Run build_candidate with scripted gate outcomes: a list of 'structural', 'functional' or 'pass'."""
    from pipeline import candidate
    from pipeline.gates import GateResult

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    atomic_write_json(tdir / "spec.json", {"slug": "demo", "title": "t", "description": "d", "requirements": []})
    atomic_write_json(tdir / "reviews" / "spec.json", {"rounds": [], "done": True})
    files = {
        "environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    assert True\n",
        "instruction.md": "Do it.\n",
    }
    script = list(outcomes)
    counters = {"gates": 0, "repairs": 0}
    prompts = []  # which repair prompt each repair used

    def fake_chain(tdir, task_path, version, seed, run_name, task_id, spec=None):
        counters["gates"] += 1
        outcome = script.pop(0)
        if outcome == "structural":
            yield GateResult("structural", False, "bad format", "fix the format")
            return
        yield GateResult("structural", True, "ok")
        if outcome == "build":
            yield GateResult("functional", False, "environment did not build or start", "build log")
            return
        details = {}
        if outcome.startswith("functional:"):  # "functional:6" = six of eight tests failing
            failing = int(outcome.split(":")[1])
            details = {"oracle": [{"tests": [{"status": "failed"}] * failing + [{"status": "passed"}] * (8 - failing)}]}
        yield GateResult("functional", outcome == "pass", "ok" if outcome == "pass" else "oracle failed", "report", details)

    def fake_repair(llm, spec, files, failure, log_dir, prompt_name="repair"):
        prompts.append(prompt_name)
        counters["repairs"] += 1
        return {"instruction.md": f"Do it. v{counters['repairs']}\n"}

    monkeypatch.setattr(candidate, "_gate_chain", fake_chain)
    monkeypatch.setattr(candidate.author, "write_environment", lambda *a, **k: dict(files))
    monkeypatch.setattr(candidate.author, "write_tests", lambda *a, **k: {})
    monkeypatch.setattr(candidate.author, "write_repair", fake_repair)
    run = lambda: candidate.build_candidate(store, "t000", seed, None, [])  # noqa: E731
    run.prompts = prompts
    return run, store, counters


def test_a_task_killed_while_its_files_are_written_does_not_pay_for_them_again(tmp_path, monkeypatch):
    """Found by the kill-and-resume test (rb1): the environment and tests calls were repeated on resume."""
    from pipeline import candidate

    run, store, _ = _repair_setup(tmp_path, monkeypatch, ["pass"])
    calls = []
    files = {"environment/Dockerfile": "FROM debian:bookworm-slim\nWORKDIR /app\n", "solution/solve.sh": "#!/bin/bash\ntrue\n"}
    tests = {"tests/test_outputs.py": "def test_a():\n    assert True\n", "instruction.md": "Do it.\n"}
    monkeypatch.setattr(candidate.author, "write_environment", lambda *a, **k: calls.append("environment") or dict(files))

    def killed(*a, **k):
        calls.append("tests")
        raise KeyboardInterrupt  # the process dies here, after the environment call returned

    monkeypatch.setattr(candidate.author, "write_tests", killed)
    with pytest.raises(KeyboardInterrupt):
        run()
    tdir = store.task_dir("t000")
    assert read_json(tdir / "parts" / "environment.json")["files"] == files and not (tdir / "v1").exists()

    monkeypatch.setattr(candidate.author, "write_tests", lambda *a, **k: calls.append("tests") or dict(tests))
    state = run()
    assert state["status"] == "ready" and calls == ["environment", "tests", "tests"]  # the environment was read back
    assert read_json(tdir / "parts" / "tests.json")["files"] == tests

    # A run that had got as far as the tests reads both back and calls nothing.
    import shutil

    shutil.rmtree(tdir / "v1")
    atomic_write_json(tdir / "state.json", {"task_id": "t000", "status": "new", "version": 0})
    assert run()["status"] == "ready" and calls == ["environment", "tests", "tests"]


def test_one_functional_repair_then_reject(tmp_path, monkeypatch):
    monkeypatch.setattr("generator.config.MAX_FUNCTIONAL_REPAIRS", 1)  # the mechanics, at the limit this test was written for
    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["functional", "functional", "pass"])
    state = run()
    assert state["status"] == "rejected" and state["rejected_at"] == "functional"
    assert state["limit"] == "MAX_FUNCTIONAL_REPAIRS" and state["version"] == 2
    assert counters == {"gates": 2, "repairs": 1}
    reject = [e for e in store.events() if e["stage"] == "reject"][0]
    assert reject["limit"] == "MAX_FUNCTIONAL_REPAIRS" and reject["failures"] == 2


def test_an_oracle_failure_gets_two_repairs_by_default(tmp_path, monkeypatch):
    """One further repair rescued 4 of 16 rejected tasks , so the limit is two."""
    from generator import config

    assert config.MAX_FUNCTIONAL_REPAIRS == 2
    run, _, counters = _repair_setup(tmp_path / "a", monkeypatch, ["functional", "functional", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 3 and counters["repairs"] == 2
    run, store, counters = _repair_setup(tmp_path / "b", monkeypatch, ["functional", "functional", "functional", "pass"])
    state = run()
    assert state["status"] == "rejected" and state["limit"] == "MAX_FUNCTIONAL_REPAIRS" and counters["repairs"] == 2


def test_functional_repair_that_works_is_kept(tmp_path, monkeypatch):
    run, _, counters = _repair_setup(tmp_path, monkeypatch, ["functional", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 2 and counters["repairs"] == 1


def test_structural_repair_does_not_use_up_the_functional_one(tmp_path, monkeypatch):
    run, _, counters = _repair_setup(tmp_path, monkeypatch, ["structural", "functional", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 3 and counters["repairs"] == 2


def test_second_structural_failure_rejects(tmp_path, monkeypatch):
    run, _, _ = _repair_setup(tmp_path, monkeypatch, ["structural", "structural"])
    state = run()
    assert state["status"] == "rejected" and state["limit"] == "MAX_STRUCTURAL_REPAIRS"


def test_resume_mid_repair_gets_no_extra_repair(tmp_path, monkeypatch):
    monkeypatch.setattr("generator.config.MAX_FUNCTIONAL_REPAIRS", 1)  # the mechanics, at the limit this test was written for
    from pipeline import candidate

    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["functional", "functional", "pass"])
    real_repair = candidate.author.write_repair
    monkeypatch.setattr(candidate.author, "write_repair", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run()  # killed while writing the one allowed repair
    monkeypatch.setattr(candidate.author, "write_repair", real_repair)

    state = run()  # resumes at version 1's stored failure, repairs once, fails again, stops
    assert state["status"] == "rejected" and state["version"] == 2
    assert counters == {"gates": 2, "repairs": 1}


# --- Concurrency --------------------------------------------------------------


def test_harbor_job_limit_is_never_exceeded():
    import threading
    import time

    from pipeline.harbor_runner import _JobLimiter

    limiter = _JobLimiter(4)

    def job():
        with limiter:
            time.sleep(0.02)

    threads = [threading.Thread(target=job) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert limiter.total == 20 and limiter.peak <= 4 and limiter.running == 0
    limiter.set_limit(2)
    assert limiter.limit == 2


def test_rate_limited_calls_are_retried_and_reported(monkeypatch):
    from types import SimpleNamespace

    from generator import llm as llm_module

    class RateLimitError(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "0"})

    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(model_dump=lambda: {"cost": 0.01, "prompt_tokens": 1, "completion_tokens": 1}),
        model="anthropic/claude-opus-5.5",
    )
    answers = [RateLimitError("slow down"), reply]

    def create(**kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(llm_module, "RETRY_DELAYS_SEC", (0, 0, 0))
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    client = llm_module.LLM.__new__(llm_module.LLM)
    client.model = "anthropic/claude-opus-5.5"
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    seen = []
    client.on_retry = lambda **fields: seen.append(fields)

    assert client.chat("hi", purpose="spec").text == "ok"
    assert len(seen) == 1 and seen[0]["rate_limited"] is True and seen[0]["purpose"] == "spec"


# --- Pitfalls and instructions must not give the task away --------------------


def _spec_with_knowable(source):
    import json

    return json.dumps({
        "slug": "a-b-c", "title": "t", "description": "d", "scenario": "s", "environment": [{"path": "/app/x"}],
        "solution_outline": ["do"],
        "requirements": [{"id": f"R{i}", "text": "x"} for i in range(4)],
        "pitfalls": [{"what": "w", "knowable_from": "the output of `ls -la /app/data`"}, {"what": "w2", "knowable_from": source}],
    })


def test_pitfall_must_be_knowable_from_the_container():
    for good in (
        "/app/docs/CHANGES.md describes the old field order",
        "running the legacy tool on a sample shows it rounds half to even",
        "requirements.txt pins the old parser version",
        # Naming a requirement or the scenario beside a real container source is fine.
        "R3 fixes the version format; running the tool on a pre-release version shows it is accepted",
        "The scenario mentions drift; comparing local_ts and utc_ts in /app/data/ingest_log.csv gives the true offset",
        "The requirement states where the binary goes. `go env GOPATH` in the container shows the actual directory",
    ):
        assert author._parse_spec(_spec_with_knowable(good))["slug"] == "a-b-c", good
    # Rejected only when what the solver is told is the sole source.
    for bad in (
        "Stated in the scenario",
        "The task description mentions it explicitly",
        "R1 explicitly states the sign rules for division and modulo",
        "The instruction says so",
        "",
    ):
        with pytest.raises(author.AuthoringError):
            author._parse_spec(_spec_with_knowable(bad))


def test_difficulty_sources_include_experiment_and_downweight_edge_cases():
    from collections import Counter

    from generator.taxonomy import DIFFICULTY_SOURCES, DIFFICULTY_WEIGHTS

    assert "needs-experiment" in DIFFICULTY_SOURCES and set(DIFFICULTY_WEIGHTS) == set(DIFFICULTY_SOURCES)
    assert "performance-limit" not in DIFFICULTY_SOURCES  # nothing timing-dependent
    counts = Counter(sample_seed("weights", i).difficulty_source for i in range(300))
    assert counts["stated-edge-cases"] < min(counts[s] for s in DIFFICULTY_SOURCES if s != "stated-edge-cases")


GIVEAWAY_SPEC = {"pitfalls": [{"what": "The archive member order follows filesystem directory order, so two builds differ unless members are sorted"}]}


def test_instruction_check_flags_hints_locations_counts_and_pitfall_wording():
    check = lambda text: quality.instruction_giveaways(text, GIVEAWAY_SPEC)  # noqa: E731

    clean = "Two consecutive builds give me a different libcrypto.a. I need the archive to be byte-for-byte identical every time, at /app/libcrypto.a."
    assert not check(clean)["tripped"]

    kinds = lambda text: [f["kind"] for f in check(text)["findings"]]  # noqa: E731
    assert kinds("The packet format is documented in /docs/packet_format.md and the policy lives in /etc/policy.toml.") == ["location"]
    assert kinds("One thing I've noticed is that the checksums end up in a bad state.") == ["hint"]
    assert kinds("Run the verification tool and inspect go.sum carefully before building.") == ["hint"]
    assert kinds("There are three bugs in the pipeline.") == ["count"]
    assert kinds("The archive member order follows filesystem directory order so two builds differ.") == ["pitfall"]


def test_instruction_with_giveaways_is_rewritten_once(tmp_path, monkeypatch):
    from pipeline import candidate

    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    files = {"instruction.md": "The build is not reproducible. Be careful with the archive step.\n", "tests/test_outputs.py": "def test_a():\n    pass\n"}
    calls = []

    def fake_rewrite(llm, instruction, tests, findings, log_dir):
        calls.append(findings)
        return "The build is not reproducible. I need identical archives from two builds.\n"

    monkeypatch.setattr(candidate.author, "rewrite_instruction", fake_rewrite)
    out = candidate._checked_instruction(store, "t000", tdir, GIVEAWAY_SPEC, files, None)
    assert "Be careful" not in out and len(calls) == 1 and calls[0][0]["kind"] == "hint"
    event = store.events()[-1]
    assert (event["stage"], event["outcome"], event["found"], event["remaining"]) == ("instruction_check", "rewritten_clean", 1, 0)
    # Resuming returns the stored result without another call.
    assert candidate._checked_instruction(store, "t000", tdir, GIVEAWAY_SPEC, files, None) == out and len(calls) == 1


def test_instruction_check_continues_when_giveaways_remain(tmp_path, monkeypatch):
    from pipeline import candidate

    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    files = {"instruction.md": "Be careful with the archive step here.\n", "tests/test_outputs.py": ""}
    monkeypatch.setattr(candidate.author, "rewrite_instruction", lambda *a, **k: "Still, be careful with the archive step.\n")
    out = candidate._checked_instruction(store, "t000", tdir, GIVEAWAY_SPEC, files, None)
    assert out.startswith("Still") and store.events()[-1]["outcome"] == "giveaways_remain"


def test_clean_instruction_is_not_rewritten(tmp_path, monkeypatch):
    from pipeline import candidate

    store = RunStore(tmp_path / "run")
    files = {"instruction.md": "Two builds give different archives. I need them identical.\n", "tests/test_outputs.py": ""}
    monkeypatch.setattr(candidate.author, "rewrite_instruction", lambda *a, **k: pytest.fail("must not be called"))
    assert candidate._checked_instruction(store, "t000", store.task_dir("t000"), GIVEAWAY_SPEC, files, None) == files["instruction.md"]
    assert store.events()[-1]["outcome"] == "clean"


def test_mistyped_file_markers_are_still_parsed():
    reply = (
        "<<<FILE environment/Dockerfile>>>\nFROM x\n<<<END>>>\n"
        "<<<FILE environment/notes/design.txt>>=\nnotes\n<<<END>>>\n"
        "<<<FILE environment/src/a.js>>`\nconst a = 1;\n<<<END>>>\n"
    )
    files = author.parse_file_blocks(reply)
    assert files == {
        "environment/Dockerfile": "FROM x\n",
        "environment/notes/design.txt": "notes\n",
        "environment/src/a.js": "const a = 1;\n",
    }


# --- Unsolved tasks: hard or broken? ------------------------------------------


def test_unsolved_task_is_broken_only_when_most_audits_blame_it():
    from pipeline.analysis import classify_unsolved

    def analysis(labels, same):
        return {
            "runs": [{} for _ in labels],
            "task_fault_votes": sum(l.startswith("task:") for l in labels),
            "tests_failed_in_every_failed_run": same,
            "median_run_minutes": 3.0,
        }

    model, task = "model:missed_requirement", "task:unstated"
    assert classify_unsolved(analysis([task] * 4 + [model], ["test_a"]))[0] == "broken"
    assert classify_unsolved(analysis([task] * 3 + [model] * 2, []))[0] == "broken"
    kind, detail = classify_unsolved(analysis([task] * 2 + [model] * 3, ["test_a", "test_b"]))
    assert kind == "too_hard" and "audits disagree" in detail and "same 2 test(s)" in detail
    kind, detail = classify_unsolved(analysis([model] * 5, []))
    assert kind == "too_hard" and "disagree" not in detail and "0 of 5 audits" in detail


def test_all_prompts_still_format():
    seed = sample_seed("prompts", 0)
    author._prompt("pitch", n_pitches=4, seed=seed.describe(), avoid="- x")
    spec = author._prompt("spec", seed=seed.describe(), pitch_title="t", pitch_summary="s", pitch_hard="h")
    assert "Keep it small." in spec
    author._prompt("spec_review", seed=seed.describe(), spec="{}")
    revise = author._prompt("spec_revise", seed=seed.describe(), spec="{}", problems="- p", req_low=4, req_high=6, pit_low=2, pit_high=3)
    assert "Do not add requirements" in revise
    author._prompt("environment", spec="{}", base_image="debian:bookworm-slim")
    author._prompt("tests", spec="{}", files="f")
    author._prompt("repair", spec="{}", files="f", failure="x")
    author._prompt("repair_build", spec="{}", files="f", failure="x")
    author._prompt("alignment", instruction="i", tests="t", environment="e")
    author._prompt("instruction_rewrite", instruction="i", findings="- f", tests="t")
    text = author._prompt(
        "run_analysis", outcome_upper="FAILED", instruction="i", tests="t", test_results="r", scan="s", transcript="x", guidance="g"
    )
    assert "Do not assume the hidden tests are right" in text


def test_running_out_of_docker_networks_is_infrastructure_not_a_broken_task():
    from pipeline.gates import _is_infrastructure_failure

    no_network = _trial(None, "RuntimeError", report=False)
    no_network.exception_message = "docker compose up failed: failed to create network x_default: could not find an available, non-overlapping IPv4 address pool"
    assert _is_infrastructure_failure(no_network)
    bad_build = _trial(None, "RuntimeError", report=False)
    bad_build.exception_message = "failed to solve: dockerfile parse error on line 31"
    assert not _is_infrastructure_failure(bad_build)


# --- No answer keys in the container ---------------------------------------------

from pipeline.answer_keys import answer_key_findings, names_an_answer  # noqa: E402


def _task_with(tmp_path, dockerfile_body, extra_files=None, instruction="The totals are wrong. Write /app/out.csv.\n"):
    """A task directory whose Dockerfile is FROM + WORKDIR /app + ``dockerfile_body``."""
    seed = sample_seed("lean", 0)
    base = STACKS[seed.stack]["base_image"]
    files = {
        "environment/Dockerfile": f"FROM {base}\nWORKDIR /app\n{dockerfile_body}",
        "environment/tool.py": "print('tool')\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    assert True\n",
        "instruction.md": instruction,
    }
    files.update(extra_files or {})
    author.assemble_task(tmp_path, {"slug": "demo", "description": "d"}, seed, files)
    return tmp_path, base


def _rules(task):
    return sorted({f["rule"] for f in answer_key_findings(task)})


def test_footer_always_removes_the_build_directory(tmp_path):
    task, _ = _task_with(tmp_path / "t", "COPY tool.py /app/\n")
    dockerfile = (task / "environment" / "Dockerfile").read_text()
    # The model's part comes first; the pipeline's footer ends by removing /tmp/_build.
    assert dockerfile.rstrip().endswith("RUN rm -rf /tmp/_build")
    assert dockerfile.count("rm -rf /tmp/_build") == 1
    assert dockerfile.index("COPY tool.py") < dockerfile.index(author.DOCKERFILE_FOOTER_MARKER) < dockerfile.index("rm -rf /tmp/_build")
    # The footer is code-owned: what the model is shown and may edit does not include it.
    assert "/tmp/_build" not in author.strip_code_owned("environment/Dockerfile", dockerfile)


def test_generator_under_build_dir_is_accepted(tmp_path):
    from pipeline.gates import structural_gate

    task, base = _task_with(
        tmp_path / "t",
        "COPY tool.py /app/\nCOPY _build/ /tmp/_build/\nRUN python3 /tmp/_build/generate_data.py\n",
        {"environment/_build/generate_data.py": "open('/app/data.csv', 'w').write('a,b\\n')\n"},
    )
    assert answer_key_findings(task) == []
    assert structural_gate(task, base).passed


def test_generator_outside_build_dir_fails_whether_or_not_it_is_deleted(tmp_path):
    from pipeline.gates import structural_gate

    left, base = _task_with(
        tmp_path / "left", "COPY tool.py seed.py /app/\nRUN python3 /app/seed.py\n", {"environment/seed.py": "# 5 orphaned rows\n"}
    )
    finding = answer_key_findings(left)[0]
    assert finding["rule"] == "generator_outside_build_dir" and finding["left_in_image"] is True
    result = structural_gate(left, base)
    assert not result.passed and "environment/_build/" in result.failure_report and "left in the image" in result.failure_report

    deleted, _ = _task_with(
        tmp_path / "deleted",
        "COPY tool.py generate_data.py /app/\nRUN python3 generate_data.py && rm generate_data.py\n",
        {"environment/generate_data.py": "pass\n"},
    )
    finding = answer_key_findings(deleted)[0]
    # Deleted in the build, so nothing is visible; still not where code can guarantee its removal.
    assert finding["rule"] == "generator_outside_build_dir" and finding["left_in_image"] is False
    assert not structural_gate(deleted, base).passed


def test_ordinary_files_are_not_mistaken_for_generators(tmp_path):
    task, _ = _task_with(
        tmp_path / "t",
        "COPY . /app\nRUN python3 /app/general_utils.py && python3 /app/genome.py\n",
        {"environment/general_utils.py": "pass\n", "environment/genome.py": "pass\n", "environment/seedling.txt": "x\n"},
    )
    assert answer_key_findings(task) == []
    # A generator-like name that no RUN line executes is just a file.
    idle, _ = _task_with(tmp_path / "idle", "COPY tool.py /app/\n", {"environment/generate_report.py": "pass\n"})
    assert answer_key_findings(idle) == []


def test_build_dir_may_only_be_copied_to_tmp_build(tmp_path):
    gen = {"environment/_build/make_data.py": "pass\n"}
    elsewhere, _ = _task_with(tmp_path / "a", "COPY _build/ /app/_build/\nRUN python3 /tmp/_build/make_data.py\n", gen)
    assert _rules(elsewhere) == ["build_dir_copied_elsewhere"]
    # Copying the whole context drags _build/ into the image as well.
    everything, _ = _task_with(tmp_path / "b", "COPY . /app\nCOPY _build/ /tmp/_build/\nRUN python3 /tmp/_build/make_data.py\n", gen)
    assert _rules(everything) == ["build_dir_copied_elsewhere"]
    # Without a _build/ directory, copying everything is fine.
    plain, _ = _task_with(tmp_path / "c", "COPY . /app\n")
    assert answer_key_findings(plain) == []


def test_build_script_must_be_run_from_tmp_build(tmp_path):
    task, _ = _task_with(
        tmp_path / "t",
        "COPY _build/ /tmp/_build/\nRUN cp -r /tmp/_build /opt/_build && python3 /opt/_build/make_data.py\n",
        {"environment/_build/make_data.py": "pass\n"},
    )
    assert "build_script_run_outside_tmp" in _rules(task)
    ok, _ = _task_with(
        tmp_path / "ok", "COPY _build/ /tmp/_build/\nRUN cd /tmp/_build && python3 make_data.py\n", {"environment/_build/make_data.py": "pass\n"}
    )
    assert answer_key_findings(ok) == []


def test_names_that_mark_an_expected_result():
    for path in ("expected_output.csv", "data/reports/weekly_reference.csv", "golden/out.json", "answers.txt", "ExpectedTotals.json", "/app/expected_output.csv"):
        assert names_an_answer(path), path
    # "reference" in documentation, and ordinary words that merely contain these letters, are not answers.
    for path in ("docs/api-reference.md", "references/paper.bib", "REFERENCE.md", "unexpected_errors.log", "preferences.json", "answering_machine.py", "tool.py"):
        assert names_an_answer(path) is None, path


def test_shipped_answer_files_fail_the_structural_gate(tmp_path):
    from pipeline.gates import structural_gate

    shipped, base = _task_with(tmp_path / "a", "COPY . /app\n", {"environment/expected_output.csv": "a,b\n1,2\n"})
    assert _rules(shipped) == ["answer_file"]
    result = structural_gate(shipped, base)
    assert not result.passed and "expected result" in result.failure_report and result.details["answer_keys"]

    pointed, _ = _task_with(
        tmp_path / "b", "COPY tool.py /app/\n", instruction="Output is wrong. Make it match `/app/expected_output.csv` exactly.\n"
    )
    assert _rules(pointed) == ["instruction_names_answer_file"]

    # The file can also be written by a build step, so it never appears among the environment files.
    written, _ = _task_with(
        tmp_path / "c",
        "COPY tool.py /app/\nCOPY _build/ /tmp/_build/\nRUN python3 /tmp/_build/make_data.py\n",
        {"environment/_build/make_data.py": "open('/app/expected_output.csv', 'w').write('1\\n')\n"},
    )
    assert _rules(written) == ["answer_file_written_at_build"]

    docs, _ = _task_with(tmp_path / "d", "COPY . /app\n", {"environment/docs/api-reference.md": "# API\n"})
    assert answer_key_findings(docs) == []


def test_comments_and_documents_that_state_a_pitfall_are_noted_not_rejected(tmp_path, monkeypatch):
    from pipeline import candidate
    from pipeline.gates import GateResult

    spec = {"requirements": [], "pitfalls": [{"what": "The archive member order follows filesystem directory order, so two builds differ unless members are sorted"}]}
    environment = {
        "environment/Dockerfile": "FROM x\n# archive member order follows filesystem directory order so two builds differ\n",
        "environment/build.sh": "#!/bin/sh\n# NOTE: archive member order follows filesystem directory order, so two builds differ\nar rcs lib.a *.o\n",
        "environment/README.md": "Build with make.\n\nThe archive member order follows filesystem directory order, so two builds differ unless sorted.\n",
        "environment/main.c": "int main(void) { return 0; } // entry point\n",
        "environment/_build/gen.py": "# archive member order follows filesystem directory order so two builds differ\n",
    }
    notes = quality.environment_giveaways(environment, spec)
    # The comment in the script and the sentence in the README are found; the Dockerfile and _build/ are skipped.
    assert notes["tripped"] and sorted(f["file"] for f in notes["findings"]) == ["environment/README.md", "environment/build.sh"]
    assert not quality.environment_giveaways({"environment/main.c": environment["environment/main.c"]}, spec)["tripped"]

    # Advisory only: it is recorded with the gate results and the task still passes.
    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    task = _minimal_task(tdir / "v1" / "demo", seed, **{"environment/build.sh": environment["environment/build.sh"]})
    monkeypatch.setattr(candidate, "functional_gate", lambda *a, **k: GateResult("functional", True, "ok"))
    result = candidate._run_gates(store, "t000", tdir, task, 1, seed, spec)
    recorded = read_json(tdir / "gates" / "v1.json")["notes"]["environment_check"]
    assert result.passed and recorded["tripped"] and recorded["findings"][0]["file"] == "environment/build.sh"
    assert [e["stage"] for e in store.events()] == ["structural", "functional"]  # no repair, no rejection


# --- Review fixes, part A ---------------------------------------------------------


def _gate_runs(monkeypatch, outcomes):
    """Make functional_gate see scripted Harbor results; returns the list of waits it slept."""
    from pipeline import gates

    script = list(outcomes)
    waits = []

    def fake_run_job(task_dir, agent, jobs_dir, job_name, **kwargs):
        kind = script[0] if agent == "oracle" else script.pop(0)
        n = kwargs.get("n_attempts", 1)
        if kind == "no_network":
            trial = _trial(None, "RuntimeError", report=False)
            trial.exception_message = "failed to create network x_default: could not find an available, non-overlapping IPv4 address pool"
            return [trial] * n, ""
        if kind == "no_result":
            return [], "harbor exited: connection reset by peer"
        if kind == "build_failure":
            trial = _trial(None, "RuntimeError", report=False)
            trial.exception_message = "failed to solve: dockerfile parse error on line 12"
            return [trial] * n, ""
        passed = agent == "oracle"
        trial = _trial(1.0 if passed else 0.0, failed=0 if passed else 1)
        return [trial] * n, ""

    monkeypatch.setattr(gates, "run_job", fake_run_job)
    monkeypatch.setattr(gates.time, "sleep", lambda seconds: waits.append(seconds))
    return gates, waits


def test_infrastructure_failure_is_retried_with_growing_waits_then_passes(tmp_path, monkeypatch):
    gates, waits = _gate_runs(monkeypatch, ["no_network", "no_result", "ok"])
    result = gates.functional_gate(tmp_path, tmp_path / "jobs", "tag")
    assert result.passed and waits == [60, 120] and result.details["infrastructure_retries"] == 2


def test_persistent_infrastructure_failure_raises_instead_of_failing_the_task(tmp_path, monkeypatch):
    gates, waits = _gate_runs(monkeypatch, ["no_network"] * 4)
    with pytest.raises(gates.InfrastructureError, match="no free network"):
        gates.functional_gate(tmp_path, tmp_path / "jobs", "tag")
    assert waits == [60, 120, 240]


def test_a_real_build_failure_is_still_the_tasks_fault(tmp_path, monkeypatch):
    gates, waits = _gate_runs(monkeypatch, ["build_failure"])
    result = gates.functional_gate(tmp_path, tmp_path / "jobs", "tag")
    assert not result.passed and result.reason == "environment did not build or start" and waits == []


def test_infrastructure_failure_writes_no_gate_file_uses_no_repair_and_stays_resumable(tmp_path, monkeypatch):
    from pipeline import candidate, run as run_module
    from pipeline.gates import GateResult, InfrastructureError

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    atomic_write_json(tdir / "spec.json", {"slug": "demo", "title": "t", "description": "d", "requirements": []})
    atomic_write_json(tdir / "reviews" / "spec.json", {"rounds": [], "done": True})
    base = STACKS[seed.stack]["base_image"]
    files = {
        "environment/Dockerfile": f"FROM {base}\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": "def test_a():\n    assert True\n",
        "instruction.md": "Do it.\n",
    }
    monkeypatch.setattr(candidate.author, "write_environment", lambda *a, **k: dict(files))
    monkeypatch.setattr(candidate.author, "write_tests", lambda *a, **k: {})
    repairs = []
    monkeypatch.setattr(candidate.author, "write_repair", lambda *a, **k: repairs.append(1) or {})
    docker_down = {"yes": True}

    def gate(*args, **kwargs):
        if docker_down["yes"]:
            raise InfrastructureError("Docker had no free network for the trial (after 4 tries)")
        return GateResult("functional", True, "ok")

    monkeypatch.setattr(candidate, "functional_gate", gate)
    monkeypatch.setattr(run_module, "sample_seed", lambda run_id, index, **kwargs: seed)
    monkeypatch.setattr(run_module, "evaluate_band", lambda *a, **k: {"passes": 5, "valid_runs": 5, "verdict": "too_easy", "complete": True})

    state = run_module.process_task(store, 0, None)
    # No verdict, nothing cached, no repair spent: the task is exactly where it was.
    assert state.get("final") is None and "InfrastructureError" in state["error"]
    assert not (tdir / "gates" / "v1.json").exists() and repairs == []
    assert read_json(tdir / "state.json").get("final") is None
    stages = [(e["stage"], e["outcome"]) for e in store.events()]
    assert ("infrastructure", "fail") in stages and ("reject", "functional") not in stages

    # Running the batch again, with Docker back, resumes at the same gate on the same version.
    docker_down["yes"] = False
    state = run_module.process_task(store, 0, None)
    assert state["final"] == "rejected:too_easy" and read_json(tdir / "state.json")["version"] == 1 and repairs == []


def test_build_dir_rule_matches_only_the_build_directory(tmp_path):
    task, _ = _task_with(tmp_path / "t", "COPY tool.py /app/\nRUN mkdir -p /app/debug_build /app/rebuild_index && touch /app/debug_build/x\n")
    assert answer_key_findings(task) == []


def test_the_tasks_own_program_is_not_a_generator(tmp_path):
    # The instruction names it: it is what the solver works on.
    named, _ = _task_with(
        tmp_path / "a",
        "COPY tool.py generate_report.py /app/\nRUN python3 /app/generate_report.py --selftest\n",
        {"environment/generate_report.py": "pass\n"},
        instruction="`python3 /app/generate_report.py` prints wrong totals. Fix it.\n",
    )
    assert answer_key_findings(named) == []
    # The tests name it.
    tested, _ = _task_with(
        tmp_path / "b", "COPY tool.py seed_cache.py /app/\nRUN python3 /app/seed_cache.py\n", {"environment/seed_cache.py": "pass\n"}
    )
    (tested / "tests" / "test_outputs.py").write_text("def test_a():\n    # runs seed_cache.py\n    assert True\n")
    assert answer_key_findings(tested) == []
    # Neither names it: it is a generator, and must live under _build/.
    hidden, _ = _task_with(
        tmp_path / "c", "COPY tool.py seed_cache.py /app/\nRUN python3 /app/seed_cache.py\n", {"environment/seed_cache.py": "pass\n"}
    )
    assert _rules(hidden) == ["generator_outside_build_dir"]


def test_generators_under_common_names_are_caught(tmp_path):
    for name in ("init_db.py", "create_fixtures.sh", "populate.py", "prepare_data.py", "mkdata.sh", "build_data.py"):
        task, _ = _task_with(
            tmp_path / name.replace(".", "_"), f"COPY tool.py {name} /app/\nRUN python3 /app/{name}\n", {f"environment/{name}": "pass\n"}
        )
        assert _rules(task) == ["generator_outside_build_dir"], name


def test_ordinary_names_are_not_answer_files():
    # Source code is never an answer file by its name.
    for path in ("dns_answer.go", "golden_ratio.py", "reference_counter.py", "cross_reference.py", "expected_value.c", "answers.sql"):
        assert names_an_answer(path) is None, path
    # "reference" alone is an ordinary word for data and documents.
    for path in ("reference_rates.csv", "data/reference_table.json", "docs/api-reference.md"):
        assert names_an_answer(path) is None, path
    # It marks an answer together with a result word, or when the solver is told to match the file.
    for path in ("reference_output.csv", "data/reports/weekly_reference.csv", "results/reference.json"):
        assert names_an_answer(path) == "reference", path
    told = "Totals drift. The new export must match reference_rates.csv exactly.\n"
    assert names_an_answer("reference_rates.csv", told) == "reference"
    assert names_an_answer("reference_rates.csv", "Rates come from reference_rates.csv. Totals drift.\n") is None
    for path in ("expected_output.csv", "golden.json", "answers.txt"):
        assert names_an_answer(path), path


def test_instruction_prose_is_not_mistaken_for_a_file(tmp_path):
    prose, _ = _task_with(
        tmp_path / "a", "COPY tool.py /app/\n",
        instruction="The report mixes up the expected/actual columns and the answer key order. Fix the tool.\n",
    )
    assert answer_key_findings(prose) == []
    path, _ = _task_with(tmp_path / "b", "COPY tool.py /app/\n", instruction="Compare with /app/expected and fix the tool.\n")
    assert _rules(path) == ["instruction_names_answer_file"]


def test_spec_revision_can_be_switched_off(tmp_path, monkeypatch):
    candidate, store, tdir, seed, calls = _spec_review_setup(tmp_path, monkeypatch, BAD_SPEC_REVIEW)
    monkeypatch.setattr(candidate.config, "MAX_SPEC_REVISIONS", 0)
    out = candidate._reviewed_spec(store, "t000", tdir, seed, None, [])
    assert "revised" not in out and calls == {"review": 1, "revise": 0}


def test_swap_files_are_ignored_by_git():
    from generator import config

    assert "*.swp" in (config.ROOT_DIR / ".gitignore").read_text()


# --- Review fixes, part B ---------------------------------------------------------


def test_record_paths_are_relative_to_the_repository_and_old_absolute_ones_still_work(tmp_path):
    from pipeline.store import REPO_ROOT, from_record_path, to_record_path

    inside = REPO_ROOT / "runs" / "b9" / "tasks" / "t000" / "v1" / "demo"
    assert to_record_path(inside) == "runs/b9/tasks/t000/v1/demo"
    assert from_record_path("runs/b9/tasks/t000/v1/demo") == inside
    # A record written before this change holds an absolute path, possibly from another checkout.
    assert from_record_path("/somewhere/else/runs/b1/tasks/t000/v1/demo") == Path("/somewhere/else/runs/b1/tasks/t000/v1/demo")
    # Outside the repository (a test's temp dir) the path is kept whole and still round-trips.
    assert from_record_path(to_record_path(tmp_path / "x")) == (tmp_path / "x").resolve()


from pathlib import Path  # noqa: E402


def _audit(label, attempt=1):
    return {"attempt": attempt, "outcome": "fail" if not label.startswith("pass") else "pass", "label": label,
            "explanation": f"says {label}", "failed_requirement": "", "evidence": [], "failed_tests": ["test_a"], "scan_hits": [], "steps": 9}


def test_majority_of_three_audits_decides():
    from pipeline.analysis import majority_audit

    saved = majority_audit([_audit("task:ambiguous"), _audit("model:missed_requirement"), _audit("model:incomplete")])
    assert saved["label"].startswith("model:") and saved["votes"] == {"acceptable": 2, "rejecting": 1} and len(saved["audits"]) == 3
    rejected = majority_audit([_audit("task:ambiguous"), _audit("task:unstated"), _audit("model:incomplete")])
    assert rejected["label"].startswith("task:") and rejected["votes"] == {"acceptable": 1, "rejecting": 2}
    # A pass that two of three audits call hard-coded is not a legitimate pass.
    hacked = majority_audit([_audit("pass:hardcoded"), _audit("pass:legitimate"), _audit("pass:hardcoded")])
    assert hacked["label"] == "pass:hardcoded"


def _analysis_setup(tmp_path, monkeypatch, labels_by_attempt):
    """analyse_task over two attempts whose successive audits return the scripted labels."""
    from pipeline import analysis

    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    band = tdir / "band" / "v1"
    atomic_write_json(band / "summary.json", {"counted_attempts": [1, 2], "passes": 1})
    for n in (1, 2):
        atomic_write_json(band / f"a{n}.json", {"attempt": n, "outcome": "fail", "trial": {"duration_sec": 120}})
    scripts = {n: list(labels) for n, labels in labels_by_attempt.items()}
    calls = []

    def fake_audit(llm, task_path, record, log_dir):
        calls.append(record["attempt"])
        return _audit(scripts[record["attempt"]].pop(0), record["attempt"])

    monkeypatch.setattr(analysis, "analyse_attempt", fake_audit)
    return analysis, store, tdir, calls


def test_one_task_fault_audit_does_not_veto_an_in_band_task(tmp_path, monkeypatch):
    analysis, store, tdir, calls = _analysis_setup(
        tmp_path, monkeypatch, {1: ["task:flaky_or_env", "model:incomplete", "model:incomplete"], 2: ["model:missed_requirement"]}
    )
    result = analysis.analyse_task(store, "t000", tdir, 1, None, revote=True)
    # Attempt 1 is audited three times and the majority clears it; attempt 2 needed one audit.
    assert result["fair"] and sorted(calls) == [1, 1, 1, 2]
    first = result["runs"][0]
    assert first["label"] == "model:incomplete" and len(first["audits"]) == 3 and "audits" not in result["runs"][1]


def test_two_of_three_task_fault_audits_still_reject(tmp_path, monkeypatch):
    analysis, store, tdir, calls = _analysis_setup(
        tmp_path, monkeypatch, {1: ["task:unstated", "task:ambiguous", "model:incomplete"], 2: ["model:missed_requirement"]}
    )
    result = analysis.analyse_task(store, "t000", tdir, 1, None, revote=True)
    assert not result["fair"] and result["runs"][0]["label"].startswith("task:") and result["task_fault_votes"] == 1


def test_unsolved_tasks_are_not_re_audited(tmp_path, monkeypatch):
    analysis, store, tdir, calls = _analysis_setup(tmp_path, monkeypatch, {1: ["task:unstated"], 2: ["model:incomplete"]})
    analysis.analyse_task(store, "t000", tdir, 1, None)  # revote off: the 0/5 path votes across attempts instead
    assert sorted(calls) == [1, 2]


def _shippable(tmp_path, monkeypatch, run_id="b9", slug="demo"):
    from pipeline import run as run_module

    monkeypatch.setattr(run_module.config, "OUTPUT_DIR", tmp_path / "output")
    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "runs" / run_id)
    task = _minimal_task(store.task_dir("t000") / "v1" / slug, seed)
    state = {"task_path": str(task), "version": 1}
    band = {"passes": 2, "valid_runs": 5}
    analysis = {"runs": [dict(_audit("model:incomplete"), audits=[{"label": "x"}], votes={"acceptable": 2, "rejecting": 1})]}
    fairness = {"fair": True, "reason": "fair", "tests": []}
    return run_module, store, state, band, analysis, fairness


def test_shipped_folder_holds_only_task_files_and_evidence_sits_beside_it(tmp_path, monkeypatch):
    run_module, store, state, band, analysis, fairness = _shippable(tmp_path, monkeypatch)
    target = run_module.ship(store, "t000", state, band, analysis, fairness, None)
    out = tmp_path / "output"
    assert target == out / "demo"
    shipped = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    # Exactly the README's layout: nothing of ours inside the task folder.
    assert shipped == ["environment/Dockerfile", "instruction.md", "solution/solve.sh", "task.toml", "tests/conftest.py", "tests/test.sh", "tests/test_outputs.py"]
    evidence = read_json(out / "_evidence" / "demo" / "verdict.json")
    assert evidence["task"] == "demo" and evidence["passes"] == 2 and evidence["run_analysis"][0]["votes"] == {"acceptable": 2, "rejecting": 1}
    # Shipping the same task again (a resumed run) reuses its folder.
    assert run_module.ship(store, "t000", state, band, analysis, fairness, None) == out / "demo"
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["_evidence", "demo"]


def test_two_tasks_with_the_same_slug_do_not_overwrite_each_other(tmp_path, monkeypatch):
    run_module, store, state, band, analysis, fairness = _shippable(tmp_path, monkeypatch, run_id="b9")
    first = run_module.ship(store, "t000", state, band, analysis, fairness, None)
    (first / "instruction.md").write_text("first task\n")
    _, other_store, other_state, *_ = _shippable(tmp_path, monkeypatch, run_id="b10")
    second = run_module.ship(other_store, "t000", other_state, band, analysis, fairness, None)
    assert second.name == "demo-2" and (first / "instruction.md").read_text() == "first task\n"
    assert read_json(tmp_path / "output" / "_evidence" / "demo-2" / "verdict.json")["run_id"] == "b10"


def test_fairness_review_fails_closed(tmp_path, monkeypatch):
    from pipeline import run as run_module

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    task = _minimal_task(tdir / "v1" / "demo", seed)
    monkeypatch.setattr(run_module.author, "review_alignment", lambda *a, **k: (_ for _ in ()).throw(author.AuthoringError("alignment review skipped tests")))
    with pytest.raises(run_module.ReviewUnavailable):
        run_module.review_fairness(store, "t000", task, 1, None)
    # Nothing is cached, so a re-run asks again; the reason is in the ledger.
    assert not (tdir / "reviews" / "alignment-v1.json").exists()
    assert [(e["stage"], e["outcome"]) for e in store.events()] == [("alignment", "unavailable")]

    # In the pipeline the task still ships (the pass count decides), with a flag saying the review is missing.
    shipped = []
    monkeypatch.setattr(run_module, "build_candidate", lambda *a, **k: {"task_id": "t000", "status": "ready", "version": 1, "task_path": str(task)})
    monkeypatch.setattr(run_module, "evaluate_band", lambda *a, **k: {"passes": 2, "valid_runs": 5, "verdict": "learnable", "complete": True})
    monkeypatch.setattr(run_module, "analyse_task", lambda *a, **k: {"fair": True, "problems": [], "runs": []})
    monkeypatch.setattr(run_module, "ship", lambda *a, **k: shipped.append(a[7]) or (tmp_path / "out" / "demo"))
    state = run_module.process_candidate(store, "t000", seed, None)
    assert state["final"] == "shipped" and state["flagged"] is True
    assert [f["kind"] for f in state["flags"]] == ["no_fairness_review"] and shipped == [state["flags"]]


# --- State generated at build time ---------------------------------------------






def test_unsolved_task_with_short_runs_is_flagged_for_a_check_by_hand():
    from pipeline.analysis import classify_unsolved

    def analysis(votes, steps):
        return {
            "task_fault_votes": votes, "runs": [{}] * 5, "tests_failed_in_every_failed_run": [],
            "median_run_minutes": 3.0, "median_steps": steps,
        }

    kind, detail = classify_unsolved(analysis(0, 7))
    assert kind == "too_hard" and "7 steps" in detail and "broken task, check by hand" in detail
    kind, detail = classify_unsolved(analysis(0, 26))
    assert kind == "too_hard" and "26 steps" in detail and "check by hand" not in detail
    assert classify_unsolved(analysis(4, 7))[0] == "broken"
    # Analyses written before steps were recorded still read.
    old = analysis(0, None)
    del old["median_steps"]
    assert classify_unsolved(old)[0] == "too_hard"


def test_report_shows_run_length_and_flags_short_unsolved_tasks(tmp_path):
    from pipeline import report

    store = RunStore(tmp_path / "run")
    for task_id, passes, episodes in (("t000", 0, [6, 7, 8, 7, 9]), ("t001", 2, [22, 30, 26, 41, 24])):
        tdir = store.task_dir(task_id)
        atomic_write_json(tdir / "state.json", {"task_id": task_id, "slug": task_id, "version": 1, "status": "ready", "final": "rejected:too_hard"})
        for n, count in enumerate(episodes, 1):
            atomic_write_json(tdir / "band" / "v1" / f"a{n}.json", {"attempt": n, "outcome": "fail", "trial": {"n_episodes": count}})
        # A summary written before steps were recorded: the report reads the attempt files.
        atomic_write_json(
            tdir / "band" / "v1" / "summary.json",
            {"complete": True, "passes": passes, "invalid_runs": 0, "verdict": "too_hard", "counted_attempts": [1, 2, 3, 4, 5]},
        )
        store.log_event(task_id, "final", "rejected:too_hard")
    data = report.collect(store)
    assert [t["median_steps"] for t in data["tasks"]] == [7, 26]
    assert data["run_length"] == {
        "tasks_measured": 2, "median_of_task_medians": 16.5, "tasks_at_20_steps_or_more": 1, "unsolved_and_short": ["t000"],
    }
    text = report.render(data)
    assert "## Run length" in text and "probably broken, not hard: t000" in text and "| Steps |" in text
    # No task was measured a second time, so the report has no column for it.
    assert "Second measurement" not in text
    atomic_write_json(store.task_dir("t001") / "confirm" / "v1" / "summary.json", {"complete": True, "passes": 3})
    assert "| Second measurement |" in report.render(report.collect(store))


def test_an_output_file_the_solver_writes_is_not_an_answer_file(tmp_path):
    """An earlier task was rejected for telling the solver to write its result to /app/bisect_answer.txt."""
    from pipeline.gates import structural_gate

    said = "The changelog is truncated. Write the hash of the commit that broke it to `/app/bisect_answer.txt`.\n"
    output, base = _task_with(tmp_path / "output", "COPY tool.py /app/tool.py\n", instruction=said)
    assert _rules(output) == [] and structural_gate(output, base).passed
    # The same name is an answer file when the container ships it, or a build step writes it.
    shipped, _ = _task_with(
        tmp_path / "shipped", "COPY bisect_answer.txt /app/bisect_answer.txt\n", {"environment/bisect_answer.txt": "abc\n"}, instruction=said
    )
    assert "instruction_names_answer_file" in _rules(shipped)
    built, _ = _task_with(
        tmp_path / "built",
        "COPY _build/ /tmp/_build/\nRUN python3 /tmp/_build/make.py\n",
        {"environment/_build/make.py": "open('/app/bisect_answer.txt', 'w').write('abc')\n"},
        instruction=said,
    )
    assert "instruction_names_answer_file" in _rules(built)


def test_fairness_review_accepts_a_quote_from_a_shipped_file_but_not_a_removed_generator():
    """An earlier task (3/5, audits fair) was rejected because the reviewer quoted the container's README, not the instruction."""
    from pipeline.quality import judge_alignment

    instruction = "The annotations are wrong. The aggregator must produce predictions for all four hostnames.\n"
    tests = 'def test_retry():\n    """Retries."""\n    assert True\n'
    environment = {
        "environment/README.md": "The aggregator queries each service. It retries up to `max_retries` times after the initial request on 503 errors.\n",
        "environment/_build/make_data.py": "# plants a collision between offsets (23,41) and (2,341) for the dedup key\n",
        "environment/Dockerfile": "FROM x\n# builds the hidden marker file for the release tag\n",
    }

    def verdict(quote="", source=""):
        review = {"tests": [{"name": "test_retry", "checks": "c", "quote": quote, "discoverable_from": source, "fair": True}]}
        return judge_alignment(instruction, tests, environment, review)

    from_readme = verdict("It retries up to max_retries times after the initial request on 503 errors")
    assert from_readme.passed and from_readme.details["tests"][0]["quote_found_in"] == "environment/README.md"
    assert verdict("The aggregator must produce predictions for all four hostnames").details["tests"][0]["quote_found_in"] == "instruction"
    # Where a quotation is found is still recorded, but no longer decides anything.
    invented = verdict("It retries forever until the service answers")
    assert invented.passed and invented.details["tests"][0]["quote_found_in"] is None
    # Text that exists only in what is removed from the image is not "found".
    assert verdict("plants a collision between offsets (23,41) and (2,341) for the dedup key").details["tests"][0]["quote_found_in"] is None
    assert verdict(source="/app/_build/make_data.py").details["tests"][0]["source_verified"] is False
    assert verdict(source="/app/README.md").details["tests"][0]["source_verified"] is True


# --- Repairs: a build failure and an oracle failure are different things --------


def test_build_repair_does_not_use_up_the_oracle_repair(tmp_path, monkeypatch):
    """b7 t001 and t009: the one repair fixed the build, the oracle then failed for the first time, and nothing was left."""
    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["build", "functional", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 3 and counters["repairs"] == 2


def test_second_build_failure_rejects(tmp_path, monkeypatch):
    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["build", "build", "pass"])
    state = run()
    assert state["status"] == "rejected" and state["rejected_at"] == "functional" and state["limit"] == "MAX_BUILD_REPAIRS"
    assert counters == {"gates": 2, "repairs": 1}


def test_one_more_repair_only_when_the_first_reduced_the_failing_tests(tmp_path, monkeypatch):
    monkeypatch.setattr("generator.config.MAX_FUNCTIONAL_REPAIRS", 1)  # the mechanics, at the limit this test was written for
    # 8 failing, then 6: the repair moved, so it gets one more, which works.
    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["functional:8", "functional:6", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 3 and counters["repairs"] == 2
    assert [e["outcome"] for e in store.events() if e["stage"] == "repair"] == ["ok", "extra", "ok"]


def test_no_extra_repair_without_progress_and_never_a_third(tmp_path, monkeypatch):
    monkeypatch.setattr("generator.config.MAX_FUNCTIONAL_REPAIRS", 1)  # the mechanics, at the limit this test was written for
    run, _, counters = _repair_setup(tmp_path / "same", monkeypatch, ["functional:6", "functional:6", "pass"])
    assert run()["status"] == "rejected" and counters["repairs"] == 1
    run, _, counters = _repair_setup(tmp_path / "worse", monkeypatch, ["functional:2", "functional:5", "pass"])
    assert run()["status"] == "rejected" and counters["repairs"] == 1
    # Progress twice still ends after the one extra repair.
    run, _, counters = _repair_setup(tmp_path / "slow", monkeypatch, ["functional:8", "functional:6", "functional:4", "pass"])
    state = run()
    assert state["status"] == "rejected" and state["version"] == 3 and counters["repairs"] == 2
    # A crash with no test report is not progress.
    run, _, counters = _repair_setup(tmp_path / "blind", monkeypatch, ["functional:8", "functional", "pass"])
    assert run()["status"] == "rejected" and counters["repairs"] == 1


def test_repair_counts_survive_a_restart_with_the_new_kinds(tmp_path, monkeypatch):
    from pipeline import candidate

    run, store, counters = _repair_setup(tmp_path, monkeypatch, ["build", "functional:8", "functional:6", "pass"])
    state = run()
    assert state["status"] == "ready" and state["version"] == 4
    assert candidate.gate_failures(store.task_dir("t000"), 4) == {"build": 1, "functional": 2}


# --- Designs: one code base, several task designs --------------------------------


def test_prompt_blocks_follow_the_design():
    text = "a\n<<IF early_build>>\nnew\n<<ELSE>>\nold\n<<END IF>>\nb\n<<IF full_reports>>\nmore\n<<END IF>>\nc"
    assert author.apply_design(text, ()) == "a\nold\nb\nc"
    assert author.apply_design(text, ("early-build",)) == "a\nnew\nb\nc"
    assert author.apply_design(text, ("full-reports",)) == "a\nold\nb\nmore\nc"
    assert author.apply_design(text, ("early-build", "full-reports")) == "a\nnew\nb\nmore\nc"
    for broken in ("<<IF a>>\nx", "<<IF a>>\n<<IF b>>\nx\n<<END IF>>\n<<END IF>>"):
        with pytest.raises(author.AuthoringError):
            author.apply_design(broken, ())


def test_every_prompt_renders_under_every_design():
    from generator import config

    fields = dict(
        n_pitches=4, avoid="- x", pitch_title="t", pitch_summary="s", pitch_hard="h", spec="{}", problems="- p",
        req_low=4, req_high=6, pit_low=2, pit_high=3, base_image="debian:bookworm-slim", files="f", failure="x",
    )
    names = ("pitch", "spec", "spec_review", "spec_revise", "environment", "tests", "repair")
    rendered = {}
    for features in ((), ("early-build",), ("full-reports",), ("public-seeds",)):
        config.set_design(features)
        seed = sample_seed("designs", 0)
        rendered[features] = {name: author._prompt(name, seed=seed.describe(), **fields) for name in names}
        for name, text in rendered[features].items():
            assert "<<IF" not in text and "<<ELSE>>" not in text and "<<END IF>>" not in text, (features, name)
    # Every prompt is rendered from the shared files, whatever the design.
    assert all(rendered[features]["spec"] for features in rendered)


def test_author_only_writes_the_task_and_runs_nothing(tmp_path, monkeypatch):
    """A pool of tasks can be written now and checked later by running the same run id again."""
    from pipeline import run as run_module

    calls = []

    def fake_build(store, task_id, seed, llm, existing, run_gates=True):
        calls.append(run_gates)
        state = {"task_id": task_id, "status": "ready" if run_gates else "authored", "version": 1, "task_path": "x", "slug": "demo"}
        atomic_write_json(store.task_dir(task_id) / "state.json", state)
        return state

    monkeypatch.setattr(run_module, "build_candidate", fake_build)
    monkeypatch.setattr(run_module, "evaluate_band", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent runs in author-only")))
    store = RunStore(tmp_path / "run")
    seed = sample_seed("pool", 0)
    out = run_module.process_candidate(store, "t000", seed, None, author_only=True)
    assert calls == [False] and out["final"] is None and "authored" in out["error"]
    assert "final" not in read_json(store.task_dir("t000") / "state.json")  # still resumable


def test_unsolved_task_with_an_unfair_test_is_recorded_as_broken(tmp_path, monkeypatch):
    """b8a: two 0/5 tasks were unfair and every run audit blamed the agent. The fairness review now runs on them too."""
    from pipeline import run as run_module

    def setup(fair):
        store = RunStore(tmp_path / ("fair" if fair else "unfair"))
        monkeypatch.setattr(run_module, "build_candidate", lambda store, task_id, *a, **k: {"task_id": task_id, "status": "ready", "version": 1, "task_path": "x"})
        monkeypatch.setattr(run_module, "evaluate_band", lambda *a, **k: {"verdict": "too_hard", "passes": 0, "valid_runs": 5})
        monkeypatch.setattr(run_module, "analyse_task", lambda *a, **k: {
            "task_fault_votes": 0, "runs": [{}] * 5, "tests_failed_in_every_failed_run": ["test_layout"], "median_run_minutes": 5.0,
            "median_steps": 30, "labels": ["model:missed_requirement"] * 5,
        })
        monkeypatch.setattr(run_module, "review_fairness", lambda *a, **k: {"fair": fair, "problems": [] if fair else ["test_layout expects something neither stated nor discoverable"]})
        return run_module.process_candidate(store, "t000", sample_seed("x", 0), None)

    hard = setup(fair=True)
    assert hard["final"] == "rejected:too_hard" and "fairness review" not in hard["reason"]
    broken = setup(fair=False)
    assert broken["final"] == "rejected:broken" and "fairness review: test_layout expects" in broken["reason"]


def test_model_call_logs_keep_distinct_numbers_when_calls_finish_together(tmp_path):
    import threading

    from generator.llm import LLM, LLMResult

    llm = object.__new__(LLM)
    llm.model = "m"
    result = LLMResult(text="x", cost_usd=0.0, prompt_tokens=1, completion_tokens=1, duration_sec=0.1, log_path=None)
    threads = [threading.Thread(target=llm._log, args=(tmp_path, "solution" if i % 2 else "tests_split", [], result, 0, True, "stop")) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(names) == 24 and len({name[:2] for name in names}) == 24


def test_a_quotation_of_two_separated_sentences_is_verified_sentence_by_sentence():
    """An earlier task (2/5, audits fair) was rejected because the reviewer quoted two bullets of the instruction as one."""
    from pipeline.quality import quote_in_container, quote_in_instruction

    instruction = (
        "- Produce `/app/output.csv` with columns `region,sample_count`, one row per region sorted by region name. "
        "A reading whose site code differs only by leading zeros must be assigned to that site.\n"
        "- Round mean and total values to four decimal places; `sample_count` is an integer with no decimal point.\n"
    )
    joined = (
        "Produce `/app/output.csv` with columns `region,sample_count`, one row per region sorted by region name. "
        "Round mean and total values to four decimal places; `sample_count` is an integer with no decimal point."
    )
    assert quote_in_instruction(joined, instruction)
    # One real sentence does not carry an invented one.
    assert not quote_in_instruction(joined.replace("four decimal places", "six decimal places"), instruction)
    assert not quote_in_instruction("Round it. To four.", instruction)  # nothing long enough to check
    assert quote_in_container(joined, {"environment/README.md": instruction, "environment/_build/gen.py": instruction}) == "environment/README.md"
    assert quote_in_container(joined, {"environment/_build/gen.py": instruction}) is None


def test_a_full_docker_disk_is_infrastructure_not_a_broken_build():
    """With Docker's disk full, every build failed and tasks were repaired and rejected for it."""
    from pipeline.gates import _infrastructure_reason

    message = "failed to solve: ResourceExhausted: write /var/lib/desktop-containerd/.../schema.sql: no space left on device"
    errored = _trial(None, "RuntimeError", report=False)
    errored.exception_message = "Docker compose command failed for environment x. " + message
    reason = _infrastructure_reason([errored, errored], [errored], "")
    assert reason and "no disk space" in reason
    # Also when Harbor gave no trial at all and the message is only in its output.
    assert "no disk space" in _infrastructure_reason([], [], "ERROR: " + message)
    # An ordinary build failure is still the task's own.
    plain = _trial(None, "RuntimeError", report=False)
    plain.exception_message = "failed to solve: process \"/bin/sh -c make\" did not complete successfully: exit code: 2"
    assert _infrastructure_reason([], [], "failed to solve: dockerfile parse error on line 3") is None


def test_reset_gates_puts_a_task_back_and_keeps_what_it_removes(tmp_path):
    import os
    import time

    from pipeline.reset_gates import reset_task

    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    old = time.time() - 3600
    for path, text in (("v1/demo/task.toml", "x"), ("v2/demo/task.toml", "y"), ("jobs/run-t000-v1-oracle/result.json", "{}")):
        (tdir / path).parent.mkdir(parents=True, exist_ok=True)
        (tdir / path).write_text(text)
    atomic_write_json(tdir / "gates" / "v1.json", {"final": {"passed": False, "gate": "functional", "reason": "environment did not build or start"}})
    atomic_write_json(tdir / "gates" / "v2.json", {"final": {"passed": False, "gate": "functional", "reason": "environment did not build or start"}})
    os.utime(tdir / "v1", (old, old))  # written long before the trouble
    atomic_write_json(tdir / "state.json", {"task_id": "t000", "status": "rejected", "version": 2, "final": "rejected:functional", "reason": "environment did not build or start", "rejected_at": "functional"})
    since = __import__("datetime").datetime.fromtimestamp(time.time() - 600, __import__("datetime").timezone.utc).isoformat()

    done = reset_task(store, "t000", since, "Docker's disk was full")
    assert done["reset"] and done["version"] == 1 and done["previous"]["final"] == "rejected:functional"
    state = read_json(tdir / "state.json")
    assert state["status"] == "authored" and state["version"] == 1 and "final" not in state and "rejected_at" not in state
    assert state["task_path"].endswith("v1/demo") and state["resets"][0]["reason"] == "Docker's disk was full"
    assert not (tdir / "v2").exists() and not list((tdir / "gates").glob("*.json")) and (tdir / "v1" / "demo" / "task.toml").exists()
    kept = next((tdir / "discarded").iterdir())
    assert (kept / "v2" / "demo" / "task.toml").read_text() == "y" and (kept / "gates" / "v1.json").exists() and (kept / "jobs").exists()
    assert [(e["stage"], e["outcome"]) for e in store.events()] == [("reset", "authored")]
    # Nothing left to undo, and a shipped task is never touched.
    assert reset_task(store, "t000", since, "again")["reset"] is False
    atomic_write_json(store.task_dir("t001") / "state.json", {"task_id": "t001", "final": "shipped", "version": 1})
    assert reset_task(store, "t001", since, "x") == {"task_id": "t001", "reset": False, "why": "shipped"}
