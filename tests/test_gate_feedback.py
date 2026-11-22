"""Design features early-build and full-reports: what a repair is told, and the build-and-retry step."""

from pathlib import Path

import pytest

from generator import author, config
from generator.taxonomy import STACKS, sample_seed
from pipeline import build_check as bc
from pipeline import candidate, gates
from pipeline.harbor_runner import TrialResult
from pipeline.store import RunStore, atomic_write_json, atomic_write_text, read_json

LOGS = Path(__file__).parent / "fixtures" / "build_logs"  # real output of `docker build --progress=plain`
SPEC = {"slug": "demo", "title": "t", "description": "d", "requirements": [], "pitfalls": []}


def test_features_are_registered_and_off_by_default():
    assert {"early-build", "full-reports"} <= set(config.DESIGN_FEATURES)
    assert not config.design_has("early-build") and not config.design_has("full-reports")
    config.set_design(("early-build", "full-reports"))
    assert config.design() == ("early-build", "full-reports")


# --- What a build repair is told ------------------------------------------------------


def test_summary_of_a_step_that_calls_a_missing_program():
    dockerfile = "FROM debian:bookworm-slim\nWORKDIR /app\nCOPY _build/ /tmp/_build/\nRUN python3 /tmp/_build/make_data.py\n"
    summary = bc.summarise_failure((LOGS / "missing_python.log").read_text(), dockerfile)
    assert summary["line"] == 4 and summary["instruction"] == "RUN python3 /tmp/_build/make_data.py" and summary["exit_code"] == 127
    assert summary["output"] == "/bin/sh: 1: python3: not found"
    text = bc.failure_text(summary, [])
    assert "Dockerfile line 4: RUN python3 /tmp/_build/make_data.py" in text and "command not found" in text and "python3: not found" in text


def test_summary_of_a_script_that_crashed_keeps_its_traceback_and_nothing_from_earlier_steps():
    summary = bc.summarise_failure((LOGS / "script_crashed.log").read_text(), "")
    assert summary["exit_code"] == 1 and summary["line"] == 3
    assert "JSONDecodeError" in summary["output"] and "building index" in summary["output"]
    assert "sha256" not in summary["output"] and "load metadata" not in summary["output"]


def test_summary_of_a_copy_of_a_file_that_is_not_there():
    summary = bc.summarise_failure((LOGS / "copy_missing.log").read_text(), "")
    assert summary["line"] == 3 and summary["instruction"] == "COPY src/ /app/src/" and summary["exit_code"] is None
    assert '"/src": not found' in summary["output"] and "load build definition" not in summary["output"]


def test_later_repairs_are_told_what_was_already_tried():
    summary = {"line": 4, "instruction": "RUN make", "exit_code": 2, "output": "make: *** No rule", "error": "x"}
    text = bc.failure_text(summary, [{"changed": ["environment/Dockerfile"], "instruction": "RUN make", "same_step": True}])
    assert "Earlier repair 1 changed environment/Dockerfile" in text and "the same step as before" in text and "already been tried" in text


# --- The build-and-retry step ---------------------------------------------------------------


def _setup(tmp_path, monkeypatch, results, repairs=None):
    """early_build with Docker and the model replaced. ``results`` is what each build returns, in order."""
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    seed = sample_seed("gf", 0)
    files = {
        "environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\nRUN python3 /tmp/make.py\n",
        "environment/README.md": "notes\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
    }
    builds, asked = [], []

    def build(context, tag):
        builds.append({"dockerfile": (context / "Dockerfile").read_text(), "files": sorted(p.name for p in context.iterdir())})
        return results.pop(0)

    def repair(llm, spec, container, failure, log_dir):
        asked.append({"failure": failure, "container": sorted(container)})
        return (repairs or [{"environment/Dockerfile": files["environment/Dockerfile"].replace("python3", "python3.12")}] * 9).pop(0)

    monkeypatch.setattr(author, "write_build_repair", repair)
    monkeypatch.setattr(bc, "remove_image", lambda tag: None)
    return store, tdir, files, build, builds, asked


FAILED = (False, (LOGS / "missing_python.log").read_text())


def test_a_container_that_builds_first_time_is_left_alone(tmp_path, monkeypatch):
    store, tdir, files, build, builds, asked = _setup(tmp_path, monkeypatch, [(True, "ok")])
    assert bc.early_build(store, "t000", tdir, SPEC, files, None, build=build) == files
    assert len(builds) == 1 and not asked
    # What is built is the task as it ships: the canary line and the pipeline's footer are on the Dockerfile.
    assert author.DOCKERFILE_FOOTER_MARKER in builds[0]["dockerfile"] and "README.md" in builds[0]["files"]
    assert [e["outcome"] for e in store.events() if e["stage"] == "build_check"] == ["pass"]


def test_a_failed_build_is_repaired_with_the_real_error_and_built_again(tmp_path, monkeypatch):
    store, tdir, files, build, builds, asked = _setup(tmp_path, monkeypatch, [FAILED, (True, "ok")])
    out = bc.early_build(store, "t000", tdir, SPEC, files, None, build=build)
    assert "python3.12" in out["environment/Dockerfile"] and out["solution/solve.sh"] == files["solution/solve.sh"]
    assert len(builds) == 2 and len(asked) == 1
    assert "python3: not found" in asked[0]["failure"] and "Exit code: 127" in asked[0]["failure"]
    # Docker said line 4 of the file it built, which has the canary line on top: line 3 of the author's own file.
    assert "Dockerfile line 3: RUN python3" in asked[0]["failure"]
    assert asked[0]["container"] == ["environment/Dockerfile", "environment/README.md"]  # the repair sees the container only
    record = read_json(tdir / "build_check.json")
    assert record["passed"] and [a["passed"] for a in record["attempts"]] == [False, True] and record["attempts"][0]["exit_code"] == 127
    assert (tdir / "build_check" / "a1" / "build.log").exists()
    # A resumed task reuses the result: nothing is built or asked again.
    again = bc.early_build(store, "t000", tdir, SPEC, files, None, build=lambda *a: pytest.fail("must not build again"))
    assert again == out


def test_a_container_that_never_builds_is_given_up_after_the_allowed_repairs(tmp_path, monkeypatch):
    tries = config.MAX_EARLY_BUILD_REPAIRS + 1
    store, tdir, files, build, builds, asked = _setup(tmp_path, monkeypatch, [FAILED] * tries)
    with pytest.raises(bc.BuildNeverPassed, match="did not build after 3 repairs"):
        bc.early_build(store, "t000", tdir, SPEC, files, None, build=build)
    assert len(builds) == tries and len(asked) == config.MAX_EARLY_BUILD_REPAIRS
    assert "Earlier repair 2 changed environment/Dockerfile" in asked[2]["failure"] and "the same step as before" in asked[2]["failure"]
    assert read_json(tdir / "build_check.json")["passed"] is False


def test_a_machine_failure_is_not_charged_to_the_task(tmp_path, monkeypatch):
    store, tdir, files, build, builds, asked = _setup(tmp_path, monkeypatch, [(False, "#5 ERROR: write /var/lib/docker/x: no space left on device")])
    with pytest.raises(gates.InfrastructureError):
        bc.early_build(store, "t000", tdir, SPEC, files, None, build=build)
    assert not asked and not (tdir / "build_check.json").exists()


def test_a_failure_in_the_pipelines_own_footer_is_left_to_the_gate(tmp_path, monkeypatch):
    store, tdir, files, build, builds, asked = _setup(tmp_path, monkeypatch, [])
    finished = author._finish_file("environment/Dockerfile", files["environment/Dockerfile"])
    footer_line = len(finished.split(author.DOCKERFILE_FOOTER_MARKER)[0].splitlines()) + 3
    log = f"#9 [5/6] RUN curl install\n#9 0.5 curl: (6) Could not resolve host\n#9 ERROR: process did not complete successfully: exit code: 6\nDockerfile:{footer_line}\n"
    assert bc.early_build(store, "t000", tdir, SPEC, files, None, build=lambda context, tag: (False, log)) == files
    assert not asked and [e["outcome"] for e in store.events() if e["stage"] == "build_check"] == ["skipped"]


def test_the_old_flow_builds_before_the_tests_are_written(tmp_path, monkeypatch):
    config.set_design(("early-build",))
    seed = sample_seed("gf", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    atomic_write_json(tdir / "spec.json", SPEC)
    atomic_write_json(tdir / "reviews" / "spec.json", {"rounds": [], "done": True})
    order = []
    env = {"environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\n", "solution/solve.sh": "#!/bin/bash\ntrue\n"}
    monkeypatch.setattr(candidate.author, "write_environment", lambda *a, **k: order.append("environment") or dict(env))

    def early(store_, task_id, tdir_, spec, files, llm):
        order.append("build")
        return {**files, "environment/Dockerfile": files["environment/Dockerfile"] + "RUN true\n"}

    def write_tests(llm, spec, files, log_dir):
        order.append("tests")
        assert files["environment/Dockerfile"].endswith("RUN true\n")  # the tests are written on the repaired container
        return {"tests/test_outputs.py": "def test_a():\n    assert True\n", "instruction.md": "Do it.\n"}

    monkeypatch.setattr(candidate.build_check, "early_build", early)
    monkeypatch.setattr(candidate.author, "write_tests", write_tests)
    state = candidate.build_candidate(store, "t000", seed, None, [], run_gates=False)
    assert order == ["environment", "build", "tests"] and state["status"] == "authored"

    # Without the feature nothing is built early.
    config.set_design(())
    order.clear()
    store2 = RunStore(tmp_path / "run2")
    atomic_write_json(store2.task_dir("t000") / "spec.json", SPEC)
    atomic_write_json(store2.task_dir("t000") / "reviews" / "spec.json", {"rounds": [], "done": True})
    monkeypatch.setattr(candidate.author, "write_tests", lambda *a, **k: order.append("tests") or {"tests/test_outputs.py": "def test_a():\n    assert True\n", "instruction.md": "Do it.\n"})
    candidate.build_candidate(store2, "t000", seed, None, [], run_gates=False)
    assert order == ["environment", "tests"]


def test_a_build_that_never_passes_rejects_the_task_as_a_build_failure(tmp_path, monkeypatch):
    config.set_design(("early-build",))
    seed = sample_seed("gf", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    atomic_write_json(tdir / "spec.json", SPEC)
    atomic_write_json(tdir / "reviews" / "spec.json", {"rounds": [], "done": True})
    monkeypatch.setattr(candidate.author, "write_environment", lambda *a, **k: {"environment/Dockerfile": "FROM x\n", "solution/solve.sh": "true\n"})

    def never(*a, **k):
        raise bc.BuildNeverPassed("the image did not build after 3 repairs; last failure at RUN make (exit code 2)")

    monkeypatch.setattr(candidate.build_check, "early_build", never)
    monkeypatch.setattr(candidate.author, "write_tests", lambda *a, **k: pytest.fail("tests must not be written for an image that does not build"))
    state = candidate.build_candidate(store, "t000", seed, None, [], run_gates=False)
    assert state["status"] == "rejected" and state["rejected_at"] == "build" and "RUN make" in state["reason"]


# --- What an oracle repair is told ----------------------------------------------------------

PYTEST_OUTPUT = """============================= test session starts ==============================
collected 3 items

../tests/test_outputs.py F.F                                             [100%]

=================================== FAILURES ===================================
_____________________________ test_report_total ________________________________

    def test_report_total():
        out = run()
>       assert out.returncode == 0, out.stderr
E       AssertionError: type error: unknown function load_data
E       assert 1 == 0

/tests/test_outputs.py:12: AssertionError
___________________________ TestFresh.test_other_input _________________________

    def test_other_input(self):
>       assert total == 7
E       assert 5 == 7

/tests/test_outputs.py:40: AssertionError
=========================== short test summary info ============================
FAILED ../tests/test_outputs.py::test_report_total - AssertionError: type error
FAILED ../tests/test_outputs.py::TestFresh::test_other_input - assert 5 == 7
========================= 2 failed, 1 passed in 0.12s ==========================
"""


def _trial(tmp_path, exit_code=None, printed="", stdout=PYTEST_OUTPUT):
    trial_dir = tmp_path / "trial"
    atomic_write_text(trial_dir / "verifier" / "test-stdout.txt", stdout)
    atomic_write_text(trial_dir / "agent" / "oracle.txt", printed)
    if exit_code is not None:  # Harbor writes it only when it is not zero
        atomic_write_text(trial_dir / "agent" / "exit-code.txt", str(exit_code))
    tests = [{"name": "test_report_total", "status": "failed", "message": ""}, {"name": "test_ok", "status": "passed", "message": ""},
             {"name": "TestFresh::test_other_input", "status": "failed", "message": ""}]  # fmt: skip
    return TrialResult(trial_name="t", trial_dir=str(trial_dir), reward=0.0, exception_type=None, exception_message=None, cost_usd=0.0,
                       n_input_tokens=0, n_output_tokens=0, n_episodes=None, duration_sec=1.0, tests=tests, has_test_report=True,
                       test_stdout_tail=stdout[-200:])  # fmt: skip


def test_report_says_first_that_the_solution_itself_crashed_and_shows_what_it_printed(tmp_path):
    report = gates.describe_oracle_failure("ORACLE run 1 did not pass", _trial(tmp_path, exit_code=1, printed="=== Report ===\ntype error: unknown function load_data\n"))
    assert "solve.sh itself FAILED: it exited with code 1" in report and "unknown function load_data" in report
    assert report.index("solve.sh itself FAILED") < report.index("did not:") < report.index("What each failed test reported")
    assert "--- test_report_total ---" in report and "--- TestFresh.test_other_input ---" in report and "assert 5 == 7" in report
    assert "short test summary" not in report and "1 test(s) passed, 2 did not" in report


def test_report_for_a_solution_that_ran_to_the_end(tmp_path):
    report = gates.describe_oracle_failure("ORACLE run 1 did not pass", _trial(tmp_path))
    assert "solve.sh ran to the end (exit code 0)" in report and "What solve.sh printed:\n(nothing)" in report


def test_every_failed_test_keeps_its_assertion_when_the_output_is_long(tmp_path):
    names = [f"test_case_{i}" for i in range(20)]
    body = "=================================== FAILURES ===================================\n" + "".join(
        f"_______ {name} _______\n\n" + "    setup line\n" * 400 + f">       assert value == {i}\nE       assert {i + 1} == {i}\n\n" for i, name in enumerate(names)
    )
    report = gates.describe_oracle_failure("ORACLE", _trial(tmp_path, stdout=body))
    assert all(f"--- {name} ---" in report for name in names) and all(f"E       assert {i + 1} == {i}" in report for i in range(20))
    assert len(report) < 30000


def test_the_fuller_report_is_used_only_under_the_feature(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(gates, "describe_oracle_failure", lambda label, trial: seen.append("full") or "FULL")
    monkeypatch.setattr(gates, "_describe_trial", lambda label, trial: seen.append("short") or "SHORT")
    trial = _trial(tmp_path)
    passing = TrialResult(**{**trial.to_dict(), "reward": 1.0, "tests": []})
    nop = TrialResult(**{**trial.to_dict(), "reward": 0.0})
    monkeypatch.setattr(gates, "run_job", lambda task_dir, agent, *a, **k: (([trial, passing] if agent == "oracle" else [nop]), ""))
    monkeypatch.setattr(gates, "_infrastructure_reason", lambda *a, **k: None)
    monkeypatch.setattr(gates, "_errored", lambda t: False)
    assert gates.functional_gate(tmp_path, tmp_path / "jobs", "x").failure_report.startswith("SHORT")
    config.set_design(("full-reports",))
    assert gates.functional_gate(tmp_path, tmp_path / "jobs", "x").failure_report.startswith("FULL")
    assert seen == ["short", "full"]


def test_a_second_repair_is_told_what_the_first_changed(tmp_path):
    tdir = tmp_path / "t000"
    for version, solve in ((1, "echo one\n"), (2, "echo two\n")):
        atomic_write_text(candidate.version_dir(tdir, version, "demo") / "solution" / "solve.sh", solve)
        atomic_write_text(candidate.version_dir(tdir, version, "demo") / "tests" / "test_outputs.py", "def test_a():\n    pass\n")
    oracle = [{"tests": [{"name": "test_a", "status": "failed"}, {"name": "test_b", "status": "passed"}]}]
    atomic_write_json(tdir / "gates" / "v1.json", {"final": {"gate": "functional", "reason": "oracle failed", "passed": False, "details": {"oracle": oracle}}})
    assert candidate.earlier_repairs_note(tdir, 1, "demo") == ""
    note = candidate.earlier_repairs_note(tdir, 2, "demo")
    assert 'Repair 1 followed "functional: oracle failed" (1 test(s) failing: test_a) and changed solution/solve.sh.' in note
    assert "Do not repeat a change that did not help" in note
