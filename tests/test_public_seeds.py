"""Public-task seeds: reading, cards, the novelty gate and the 'public-seeds' design.

The public task here is a synthetic fixture written to a temporary directory,
not Terminal-Bench data. No model is called.
"""

import json

import pytest

from generator import config, public_seeds
from generator.taxonomy import sample_seed


CARD = {
    "seed": "fixture-logs",
    "domain": "log forensics",
    "task_types": ["investigate", "debug"],
    "stack": "python",
    "skills": ["parse rotated logs", "reconstruct an event order"],
    "skill": "Reconstruct the order of events across rotated log files.",
    "verifier": "Checks the exact ordered list of event ids written to a report file.",
    "hard_part": "Agents sort by timestamp and miss records whose clocks skew between files.",
    "levers": ["more rotated files", "clock skew between hosts"],
    "keep": "The answer depends on ordering across several files.",
    "change": ["a different scenario and names", "a different output format"],
}


@pytest.fixture
def tb(tmp_path):
    """A one-task public root and a cards directory, both under tmp_path."""
    root = tmp_path / "tasks"
    t = root / "fixture-logs"
    (t / "tests").mkdir(parents=True)
    (t / "solution").mkdir()
    (t / "environment").mkdir()
    (t / "task.toml").write_text(
        '# harbor-canary GUID 00000000-0000-0000-0000-000000000000\n'
        '[metadata]\ncategory = "Debugging"\ntags = ["logs"]\n'
    )
    (t / "instruction.md").write_text(
        "<!-- harbor-canary GUID 00000000 -->\nPut the event ids in order in /app/report.txt.\n"
    )
    (t / "tests" / "test_outputs.py").write_text("def test_order():\n    assert read() == ['e1', 'e2']\n")
    (t / "solution" / "solve.sh").write_text("#!/bin/bash\necho e1 > /app/report.txt\n")
    (t / "environment" / "Dockerfile").write_text("FROM python:3.12-slim-bookworm\n")
    cards = tmp_path / "cards"
    cards.mkdir()
    return root, cards


def test_task_names_lists_only_complete_tasks(tb):
    root, _ = tb
    (root / "incomplete").mkdir()
    assert public_seeds.task_names(root) == ["fixture-logs"]


def test_read_public_task_strips_canary_lines(tb):
    root, _ = tb
    src = public_seeds.read_public_task("fixture-logs", root)
    assert "canary" not in src["instruction"].lower()
    assert src["base_image"] == "python:3.12-slim-bookworm"
    assert src["category"] == "Debugging"


def test_check_card_rejects_incomplete_cards():
    with pytest.raises(ValueError):
        public_seeds.check_card({"seed": "x", "domain": "d"})
    bad = dict(CARD, levers=["only one"])
    with pytest.raises(ValueError):
        public_seeds.check_card(bad)


def test_novelty_gate_rejects_copied_instruction_and_passes_new_one(tb, tmp_path):
    root, _ = tb
    task = tmp_path / "candidate"
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "test_outputs.py").write_text("def test_x():\n    assert 1 == 1\n")

    (task / "instruction.md").write_text("Put the event ids in order in /app/report.txt.\n")
    copied = public_seeds.novelty_gate(task, CARD, root)
    assert not copied.passed and copied.gate == "novelty"
    assert "Rewrite it in your own words" in copied.failure_report

    (task / "instruction.md").write_text(
        "A courier fleet exported shift telemetry across four rotated gateways. Recover the "
        "sequence in which the depot scanners fired and save the codes to the manifest.\n"
    )
    assert public_seeds.novelty_gate(task, CARD, root).passed


def test_draw_card_is_deterministic_and_needs_cards(tb, monkeypatch):
    _, cards = tb
    monkeypatch.setattr(config, "CARDS_DIR", cards)
    with pytest.raises(RuntimeError):
        public_seeds.draw_card("run", 0)
    (cards / "fixture-logs.json").write_text(json.dumps(CARD))
    assert public_seeds.draw_card("run", 3)["seed"] == "fixture-logs"
    assert public_seeds.draw_card("run", 3) == public_seeds.draw_card("run", 3)


def test_public_seeds_design_draws_card_based_seeds(tb, monkeypatch):
    _, cards = tb
    monkeypatch.setattr(config, "CARDS_DIR", cards)
    (cards / "fixture-logs.json").write_text(json.dumps(CARD))
    config.set_design(("public-seeds", "early-build", "full-reports"))
    try:
        seed = sample_seed("run", 0)
        assert seed.card and seed.card["seed"] == "fixture-logs"
        assert seed.domain == "log forensics"
        assert seed.task_type in CARD["task_types"]
        assert seed.difficulty_source == "public-seed"
        text = seed.describe()
        assert "Public seed" in text and "Must differ from the seed" in text
        assert "Put the event ids" not in text  # the public wording is never shown to the author
    finally:
        config.set_design(config.DEFAULT_DESIGN)


def test_public_seeds_cannot_be_combined_with_mix():
    with pytest.raises(ValueError):
        config.set_design(("public-seeds", "mix"))
    config.set_design(config.DEFAULT_DESIGN)


def test_template_parameters_are_drawn_into_the_seed(tb, monkeypatch):
    _, cards = tb
    monkeypatch.setattr(config, "CARDS_DIR", cards)
    card = dict(CARD, template={
        "parameters": [{"name": "services", "values": ["two", "five"]}],
        "skeleton": "a log of N services where clock skew decides the order",
    })
    (cards / "fixture-logs.json").write_text(json.dumps(card))
    config.set_design(("public-seeds",))
    try:
        seed = sample_seed("run", 0)
        assert seed.params["services"] in ("two", "five")
        text = seed.describe()
        assert f"services = {seed.params['services']}" in text
        assert "Template skeleton" in text
    finally:
        config.set_design(config.DEFAULT_DESIGN)


def test_custom_task_directory_can_be_read(tb, tmp_path):
    root, _ = tb
    src = public_seeds.read_public_task(str(root / "fixture-logs"))
    assert src["name"] == "fixture-logs" and src["source_dir"].endswith("fixture-logs")


def test_build_cards_accepts_a_task_path(tb, tmp_path, monkeypatch):
    root, cards = tb
    monkeypatch.setattr(config, "CARDS_DIR", cards)

    class FakeLLM:
        def chat(self, prompt, **kw):
            raise AssertionError("cached card should not call the model")

    (cards / "fixture-logs.json").write_text(json.dumps(CARD))
    assert public_seeds.build_cards(FakeLLM(), [str(root / "fixture-logs")], root) == []
