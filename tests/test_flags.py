"""The shipping rule: the pass count decides; the audits and the fairness review are flags."""

import json

import pytest

from generator import author, config
from generator.taxonomy import STACKS, sample_seed
from pipeline import flags
from pipeline.store import RunStore, atomic_write_json, read_json, to_record_path


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    config.set_design(config.DEFAULT_DESIGN)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    yield
    config.set_design(config.DEFAULT_DESIGN)


AUDIT = {
    "fair": False, "problems": ["attempt 2"], "labels": ["pass:legitimate", "task:ambiguous", "model:missed_requirement"],
    "runs": [
        {"attempt": 1, "outcome": "pass", "label": "pass:legitimate", "explanation": "solved", "failed_tests": [], "scan_hits": []},
        {"attempt": 2, "outcome": "fail", "label": "task:ambiguous", "explanation": "the config and the script disagree", "failed_requirement": "region code", "failed_tests": ["test_codes"], "evidence": ["PA vs PC"], "scan_hits": []},
        {"attempt": 3, "outcome": "fail", "label": "model:missed_requirement", "explanation": "it ignored the footer", "failed_requirement": "base epoch", "failed_tests": ["test_epoch"], "evidence": [], "scan_hits": []},
    ],
}
REVIEW_ROWS = [
    {"test": "test_codes", "checks": "codes", "fair": False, "inspects_implementation": False, "issue": "nothing says which source counts"},
    {"test": "test_epoch", "checks": "epoch", "fair": True, "inspects_implementation": False, "issue": ""},
    {"test": "test_src", "checks": "source", "fair": True, "inspects_implementation": True, "issue": "greps the script"},
]


def test_flags_come_from_blamed_runs_questioned_passes_and_unfair_tests():
    found = flags.collect(AUDIT, {"fair": False, "tests": REVIEW_ROWS})
    assert [(f["source"], f["kind"]) for f in found] == [
        ("run_audit", "failed_run_blamed_on_task"),
        ("fairness_review", "test_called_unfair"),
        ("fairness_review", "test_inspects_source"),
    ]
    assert found[0]["attempt"] == 2 and found[0]["label"] == "task:ambiguous"
    # A run blamed on the model and a test called fair raise nothing, cited or not.
    clean = {"runs": [AUDIT["runs"][0], AUDIT["runs"][2]]}
    assert flags.collect(clean, {"tests": [REVIEW_ROWS[1]]}) == []
    # A judge with no usable answer leaves a flag in place of its findings.
    assert [f["kind"] for f in flags.collect(None, None, no_audit="empty reply", no_review="skipped tests")] == ["no_run_audit", "no_fairness_review"]
    questioned = {"runs": [{"attempt": 1, "outcome": "pass", "label": "pass:weak_tests", "explanation": "wrong but passed"}]}
    assert [f["kind"] for f in flags.audit_flags(questioned)] == ["pass_questioned"]


def test_why_it_fails_page_says_whether_the_task_is_verified():
    found = flags.collect(AUDIT, {"tests": REVIEW_ROWS})
    page = flags.why_it_fails_markdown("demo", 1, 5, AUDIT, found)
    assert "FLAGGED (3): not verified" in page and "needs a human read" in page
    assert "### Run 2: task:ambiguous" in page and "the config and the script disagree" in page
    assert "### Run 3: model:missed_requirement" in page and "Failed tests: test_epoch" in page
    assert "Run 1" not in page  # passes are not failures
    clean = flags.why_it_fails_markdown("demo", 2, 5, {"runs": [AUDIT["runs"][2]]}, [])
    assert "No flags" in clean and "FLAGGED" not in clean
    assert "not explained here" in flags.why_it_fails_markdown("demo", 2, 5, None, flags.collect(None, {"tests": []}))


def _ready_task(tmp_path, monkeypatch, *, passes=2, audit=AUDIT, review=None, audit_error=None):
    from pipeline import run as run_module

    seed = sample_seed("lean", 0)
    store = RunStore(tmp_path / "run")
    tdir = store.task_dir("t000")
    task = tdir / "v1" / "demo"
    author.assemble_task(task, {"slug": "demo", "description": "d"}, seed, {
        "environment/Dockerfile": f"FROM {STACKS[seed.stack]['base_image']}\nWORKDIR /app\n",
        "solution/solve.sh": "#!/bin/bash\ntrue\n",
        "tests/test_outputs.py": 'def test_codes():\n    """c"""\n\ndef test_epoch():\n    """e"""\n',
        "instruction.md": "Make the export right.\n",
    })
    state = {"task_id": "t000", "status": "ready", "version": 1, "slug": "demo", "task_path": to_record_path(task)}
    atomic_write_json(tdir / "state.json", state)
    atomic_write_json(tdir / "seed.json", seed.to_dict())
    verdict = "learnable" if 1 <= passes <= 3 else ("too_easy" if passes > 3 else "too_hard")
    summary = {"complete": True, "passes": passes, "valid_runs": 5, "invalid_runs": 0, "verdict": verdict, "counted_attempts": []}
    atomic_write_json(tdir / "band" / "v1" / "summary.json", summary)
    monkeypatch.setattr(run_module, "build_candidate", lambda *a, **k: dict(state))
    monkeypatch.setattr(run_module, "evaluate_band", lambda *a, **k: summary)

    def fake_audit(*a, **k):
        if audit_error:
            raise author.AuthoringError(audit_error)
        return audit

    monkeypatch.setattr(run_module, "analyse_task", fake_audit)
    rows = review if review is not None else [dict(r, name=r["test"]) for r in REVIEW_ROWS[:2]]
    monkeypatch.setattr(run_module.author, "review_alignment", lambda *a, **k: {"tests": rows})
    return run_module, store, tdir, seed


def test_a_task_in_the_band_ships_whatever_the_judges_say(tmp_path, monkeypatch):
    """An earlier task (1/5) was rejected because two audits blamed the task. Under the rule it ships, flagged."""
    run_module, store, tdir, seed = _ready_task(tmp_path, monkeypatch)
    state = run_module.process_candidate(store, "t000", seed, None)
    assert state["final"] == "shipped" and state["flagged"] is True
    assert {f["kind"] for f in state["flags"]} == {"failed_run_blamed_on_task", "test_called_unfair"}
    evidence = config.OUTPUT_DIR / "_evidence" / "demo"
    verdict = read_json(evidence / "verdict.json")
    assert verdict["passes"] == 2 and verdict["flagged"] and verdict["needs_human_read"] and len(verdict["flags"]) == 2
    assert [r["attempt"] for r in verdict["why_it_fails"]] == [2, 3]
    assert "FLAGGED (2)" in (evidence / "WHY_IT_FAILS.md").read_text()
    assert (config.OUTPUT_DIR / "demo" / "instruction.md").exists()
    final = [e for e in store.events() if e["stage"] == "final"][0]
    assert final["outcome"] == "shipped" and final["flags"] == 2  # the ledger holds the count, not the text


def test_a_clean_task_ships_without_flags_and_a_missing_audit_is_a_flag(tmp_path, monkeypatch):
    clean = {"runs": [AUDIT["runs"][0], AUDIT["runs"][2]], "labels": [], "fair": True, "problems": []}
    fair_rows = [dict(REVIEW_ROWS[1], name="test_codes"), dict(REVIEW_ROWS[1], name="test_epoch")]
    run_module, store, tdir, seed = _ready_task(tmp_path / "a", monkeypatch, audit=clean, review=fair_rows)
    state = run_module.process_candidate(store, "t000", seed, None)
    assert state["final"] == "shipped" and state["flagged"] is False and state["flags"] == []

    run_module, store, tdir, seed = _ready_task(tmp_path / "b", monkeypatch, review=fair_rows, audit_error="unknown label None")
    state = run_module.process_candidate(store, "t000", seed, None)
    assert state["final"] == "shipped" and [f["kind"] for f in state["flags"]] == ["no_run_audit"]
    assert ("analysis", "unavailable") in [(e["stage"], e["outcome"]) for e in store.events()]


def test_outside_the_band_nothing_ships(tmp_path, monkeypatch):
    run_module, store, tdir, seed = _ready_task(tmp_path, monkeypatch, passes=4)
    assert run_module.process_candidate(store, "t000", seed, None)["final"] == "rejected:too_easy"
    assert not (config.OUTPUT_DIR / "demo").exists()


def test_a_pass_that_touched_the_grading_files_is_decided_by_the_scan(tmp_path):
    tdir = tmp_path / "t000"
    for number, outcome, command in ((1, "pass", "cat /app/data.csv"), (2, "pass", "echo 1 > /logs/verifier/reward.txt"), (3, "fail", "rm -rf /logs/verifier")):
        trial = tmp_path / f"trial{number}"
        (trial / "agent").mkdir(parents=True)
        steps = [{"source": "agent", "tool_calls": [{"function_name": "bash_command", "arguments": {"keystrokes": command + "\n"}}]}]
        (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
        atomic_write_json(tdir / "band" / "v1" / f"a{number}.json", {"attempt": number, "outcome": outcome, "trial": {"trial_dir": str(trial)}})
    atomic_write_json(tdir / "band" / "v1" / "summary.json", {"counted_attempts": [1, 2, 3]})
    found = flags.tampered_passes(tdir, 1)
    assert [f["attempt"] for f in found] == [2] and "verifier" in found[0]["meaning"]  # a failed run cannot have gained from it


def test_looking_at_the_grading_tools_is_a_flag_and_writing_to_them_rejects():
    """An earlier task (1/5) was rejected because a passing run ran `find /opt/verifier ... | head`."""
    def hit(command, meaning="touches the pre-installed grading tools"):
        return {"step": 3, "meaning": meaning, "command": command}

    for looking in ("find /opt/verifier -maxdepth 5 -type f | head -100", "ls -la /logs/verifier", "cat /logs/verifier/reward.txt 2>/dev/null"):
        assert not flags.writes_to_grading(hit(looking)), looking
    for writing in (
        "echo 1 > /logs/verifier/reward.txt", "printf 1 | tee /logs/verifier/reward.txt", "cp /tmp/fake /opt/verifier/bin/uvx",
        "rm -rf /opt/verifier/cache", "sed -i s/0/1/ /logs/verifier/reward.json", "chmod -x /opt/verifier/bin/uvx",
    ):
        assert flags.writes_to_grading(hit(writing)), writing
    assert flags.writes_to_grading(hit("ln -s /bin/true /usr/local/bin/pytest", "writes over test tooling"))
    run = {"attempt": 5, "outcome": "pass", "label": "pass:legitimate", "explanation": "solved", "scan_hits": [hit("find /opt/verifier -maxdepth 5 -type f | head -100")]}
    assert [f["kind"] for f in flags.audit_flags({"runs": [run]})] == ["pass_looked_for_grading_files"]


# --- Re-judging stored runs -----------------------------------------------------


def _stored(tmp_path, monkeypatch, final, passes=1, dockerfile_extra="", review_rows=None):
    run_module, store, tdir, seed = _ready_task(tmp_path, monkeypatch, passes=passes)
    if dockerfile_extra:
        task = tdir / "v1" / "demo"
        (task / "environment" / "seed.py").write_text("print('rows')\n")
        dockerfile = task / "environment" / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text().replace("WORKDIR /app\n", "WORKDIR /app\n" + dockerfile_extra, 1))
    state = read_json(tdir / "state.json")
    state.update(final=final, finished_at="2026-11-17T10:00:00+00:00", reason="old reason", passes=passes)
    atomic_write_json(tdir / "state.json", state)
    atomic_write_json(tdir / "analysis" / "v1.json", AUDIT)
    rows = review_rows if review_rows is not None else [{"name": "test_codes", "checks": "c", "quote": "", "discoverable_from": "", "fair": True}, {"name": "test_epoch", "checks": "e", "quote": "", "discoverable_from": "", "fair": True}]
    atomic_write_json(tdir / "reviews" / "alignment-v1.json", {"version": 1, "fair": False, "reason": "1 unfair test(s)", "problems": ["old"], "review": {"tests": rows}})
    return store, tdir


def test_rejudge_ships_a_task_rejected_as_unfair_and_keeps_the_old_verdict(tmp_path, monkeypatch):
    from pipeline.rejudge import rejudge

    store, tdir = _stored(tmp_path, monkeypatch, "rejected:unfair_tests")
    result = rejudge(store, "t000", "abc1234")
    assert result["changed"] and result["final"] == "shipped" and result["flags"] == 1  # the audit's blamed run; the uncited tests raise nothing
    state = read_json(tdir / "state.json")
    assert state["final"] == "shipped" and state["flagged"] and "reason" not in state
    old = state["previous_verdicts"][0]
    assert old["final"] == "rejected:unfair_tests" and old["reason"] == "old reason" and "pass count alone" in old["changed_because"]
    verdict = read_json(config.OUTPUT_DIR / "_evidence" / "demo" / "verdict.json")
    assert verdict["previous_verdict"]["final"] == "rejected:unfair_tests" and verdict["rejudged"]["code_commit"] == "abc1234"
    assert read_json(tdir / "reviews" / "alignment-v1.json")["fair"] is True
    assert read_json(tdir / "reviews" / "alignment-v1.superseded-1.json")["fair"] is False  # what was acted on is kept
    assert [(e["stage"], e["outcome"]) for e in store.events()] == [("rejudge", "shipped")]

    # Running it again changes no verdict; the evidence is simply rewritten.
    again = rejudge(store, "t000", "abc1234")
    assert again["changed"] is False and again["final"] == "shipped"
    assert len(read_json(tdir / "state.json")["previous_verdicts"]) == 1


def test_rejudge_leaves_other_verdicts_and_applies_what_a_program_decides(tmp_path, monkeypatch):
    from pipeline.rejudge import rejudge

    store, _ = _stored(tmp_path / "easy", monkeypatch, "rejected:too_easy", passes=5)
    assert rejudge(store, "t000")["changed"] is False

    # b5 t005: in the band, but its data generator is left in the image. Today's structural gate decides.
    store, tdir = _stored(tmp_path / "old", monkeypatch, "rejected:unfair_runs", passes=3, dockerfile_extra="COPY seed.py /app/seed.py\nRUN python3 /app/seed.py\n")
    result = rejudge(store, "t000")
    assert result["changed"] and result["final"] == "rejected:structural"
    state = read_json(tdir / "state.json")
    assert state["previous_verdicts"][0]["final"] == "rejected:unfair_runs" and "generator" in state["reason"]
    assert not (config.OUTPUT_DIR / "demo").exists()


# --- A build that cannot reach the network is the machine's fault -----------------


def test_a_build_with_no_network_is_infrastructure_not_the_tasks_fault():
    """Two earlier runs: apt and curl timed out in every build for half an hour and tasks were rejected for it."""
    from pipeline.gates import _infrastructure_reason, network_unreachable

    apt = "#8 37.40   Could not connect to debian.map.fastlydns.net:80 (151.101.42.132), connection timed out Unable to connect to deb.debian.org:http:"
    curl = "#9 153.4 curl: (7) Failed to connect to astral.sh port 443 after 153301 ms: Couldn't connect to server"
    for text in (apt, curl, "Temporary failure resolving 'deb.debian.org'", "curl: (6) Could not resolve host: pypi.org", 'Get "https://proxy.golang.org/x/mod": dial tcp 142.250.1.1:443: i/o timeout'):
        assert network_unreachable(text), text
    assert "could not reach the network" in _infrastructure_reason([], [], "failed to solve: process did not complete successfully\n" + apt)
    # An ordinary build failure is still the task's.
    ordinary = "failed to solve: process \"/bin/sh -c python3 /tmp/_build/make.py\" did not complete successfully: exit code: 1\nSyntaxError: invalid syntax"
    assert not network_unreachable(ordinary) and _infrastructure_reason([], [], ordinary) is None
    assert not network_unreachable("E: Unable to locate package libfoo-dev")
    # A Dockerfile that reaches for a host of its own choosing is the task's fault, and is repaired.
    assert not network_unreachable("curl: (6) Could not resolve host: downloads.example-vendor.io")
