"""Public Terminal-Bench 4 tasks as seeds.

A public task is read once and reverse-engineered into a *card*: the skill it
tests, what its verifier really checks, the step a capable agent gets wrong,
the levers a variant may change and what must stay the same. The pipeline
then writes a new task from the card. The public task's own wording, data,
names and answer never reach the output: the novelty gate in
``novelty_gate`` (below) rejects a candidate whose
instruction or tests share too many word pairs with its seed.

Cards are cached under ``seeds/cards``. The public task files live in
``seeds/terminal-bench-4`` (fetched by ``scripts/fetch_seeds.sh``, not
committed). Benchmark canary lines are stripped before anything is shown to
a model.

    bash scripts/fetch_seeds.sh
    .venv/bin/python -m generator.public_seeds list
    .venv/bin/python -m generator.public_seeds build [--only NAME ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

from generator import config
from generator.similarity import overlap
from generator.taxonomy import STACKS, TASK_TYPES
from pipeline.gates import GateResult
from pipeline.store import atomic_write_json

CARD_REQUIRED = ("domain", "task_types", "stack", "skills", "skill", "verifier", "hard_part", "levers", "keep", "change")
EXCERPT_CHARS = 6000  # per file shown to the card writer
CANARY_LINE = re.compile(r"^.*(harbor-canary|terminal-bench-canary|BENCHMARK DATA SHOULD NEVER).*$\n?", re.M)


# --- reading the public tasks -------------------------------------------------

def task_names(root: Path = config.TB4_DIR) -> list[str]:
    """Names of the public tasks present, i.e. directories with a task.toml and an instruction."""
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if (p / "task.toml").is_file() and (p / "instruction.md").is_file()
    )


def _clean(text: str) -> str:
    return CANARY_LINE.sub("", text)


def _excerpt(text: str) -> str:
    text = _clean(text)
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "\n[... truncated ...]"


def task_dir_of(name_or_path: str | Path, root: Path = config.TB4_DIR) -> Path:
    """A task directory, given either as a path or as a name under ``root``."""
    path = Path(name_or_path)
    return path if path.is_dir() else root / str(name_or_path)


def read_public_task(name: str | Path, root: Path = config.TB4_DIR) -> dict:
    """Metadata, instruction and test/solution excerpts of one task (any Harbor task directory)."""
    tdir = task_dir_of(name, root)
    name = tdir.name
    toml = tomllib.loads((tdir / "task.toml").read_text())
    meta = toml.get("metadata", {})
    tests = {}
    for path in sorted((tdir / "tests").glob("*")):
        if path.is_file() and path.name not in ("Dockerfile",):
            tests[path.name] = _excerpt(path.read_text(errors="replace"))
    solution = ""
    for path in sorted((tdir / "solution").glob("*")) if (tdir / "solution").is_dir() else []:
        if path.is_file():
            solution += f"# {path.name}\n" + _excerpt(path.read_text(errors="replace")) + "\n"
    dockerfile = tdir / "environment" / "Dockerfile"
    base = ""
    if dockerfile.is_file():
        m = re.search(r"^FROM\s+(\S+)", dockerfile.read_text(errors="replace"), re.M)
        base = m.group(1) if m else ""
    return {
        "name": name,
        "category": meta.get("category", ""),
        "subcategory": meta.get("subcategory", ""),
        "tags": list(meta.get("tags", [])),
        "instruction": _clean((tdir / "instruction.md").read_text(errors="replace")),
        "tests": tests,
        "solution": _excerpt(solution),
        "base_image": base,
        "source_dir": str(tdir.resolve()),
    }


def source_text(source: str | Path) -> dict[str, str]:
    """The seed task's instruction and test source, for the novelty gate only (never shown to an author)."""
    src = read_public_task(source)
    test_text = "\n".join(src["tests"].values())
    return {"instruction": src["instruction"], "tests": test_text}


# --- cards ---------------------------------------------------------------------

def card_path(name: str) -> Path:
    return config.CARDS_DIR / f"{name}.json"


def check_card(card: dict) -> None:
    """Raise ValueError unless the card has what the generator needs."""
    missing = [k for k in CARD_REQUIRED if not card.get(k)]
    if missing:
        raise ValueError(f"seed card {card.get('seed', '?')} lacks {missing}")
    if not isinstance(card["levers"], list) or len(card["levers"]) < 2:
        raise ValueError("a seed card needs at least two levers")
    if not isinstance(card["change"], list) or len(card["change"]) < 2:
        raise ValueError("a seed card needs at least two things to change")
    for param in template_parameters(card):
        if len(param["values"]) < 2:
            raise ValueError(f"template parameter {param['name']} needs at least two values")


def template_parameters(card: dict) -> list[dict]:
    """The parameters a generator varies to make distinct tasks from this card's skeleton."""
    return list((card.get("template") or {}).get("parameters") or [])


def load_card(name: str) -> dict | None:
    path = card_path(name)
    if not path.is_file():
        return None
    card = json.loads(path.read_text())
    check_card(card)
    return card


def available_cards() -> list[dict]:
    """Every valid card on disk, sorted by seed name."""
    cards = []
    if not config.CARDS_DIR.is_dir():
        return cards
    for path in sorted(config.CARDS_DIR.glob("*.json")):
        try:
            card = json.loads(path.read_text())
            check_card(card)
        except (ValueError, json.JSONDecodeError):
            continue
        cards.append(card)
    return cards


def draw_card(run_id: str, index: int) -> dict:
    """The card for task ``index`` of ``run_id``.

    Cards are ordered by a hash of the run id and seed name, so each run meets
    the public tasks in a different order and a resumed run keeps its order.
    """
    cards = available_cards()
    if not cards:
        raise RuntimeError(
            "no seed cards: run `bash scripts/fetch_seeds.sh` and then "
            "`.venv/bin/python -m generator.public_seeds build`"
        )
    order = sorted(cards, key=lambda c: hashlib.sha256(f"{run_id}|{c['seed']}".encode()).hexdigest())
    return order[index % len(order)]


def card_block(card: dict) -> str:
    """Seed description for prompts. Says what to keep and what to change, not the public wording."""
    lines = [
        "- Public seed (reverse-engineered; take the skill and the mechanism, never its wording, data, names or answer):",
        f"  - Skill tested: {card['skill']}",
        f"  - What the verifier really checks: {card['verifier']}",
        f"  - The step a capable agent gets wrong: {card['hard_part']}",
        "  - Levers you may change:",
        *[f"    - {x}" for x in card["levers"]],
        f"  - Must stay the same: {card['keep']}",
        "  - Must differ from the seed:",
        *[f"    - {x}" for x in card["change"]],
    ]
    skeleton = (card.get("template") or {}).get("skeleton")
    if skeleton:
        lines.append(f"  - Template skeleton: {skeleton}")
    return "\n".join(lines)


def _parse_card(text: str, name: str) -> dict:
    from generator.llm import parse_json

    card = parse_json(text)
    if not isinstance(card, dict):
        raise ValueError("the reply is not a JSON object")
    card["seed"] = name
    tmpl = card.get("template") or {}
    tmpl["parameters"] = [
        {"name": str(p["name"]), "values": [str(v) for v in p["values"]]}
        for p in (tmpl.get("parameters") or [])[:3]
        if isinstance(p, dict) and p.get("name") and isinstance(p.get("values"), list)
    ]
    card["template"] = tmpl
    check_card(card)
    if card["stack"] not in STACKS:
        raise ValueError(f"stack must be one of {sorted(STACKS)}")
    card["task_types"] = [t for t in card["task_types"] if t in TASK_TYPES] or ["implement"]
    return card


def reverse_engineer(llm, name: str, log_dir: Path, root: Path = config.TB4_DIR) -> dict:
    """Ask the model for one card. Cached on success by ``build_cards``."""
    from generator.author import _ask  # lazy: author imports taxonomy, which imports this module

    src = read_public_task(name, root)
    name = src["name"]
    tests = "\n\n".join(f"### tests/{k}\n{v}" for k, v in src["tests"].items())
    prompt = (config.PROMPTS_DIR / "seed_card.md").read_text().format(
        name=name,
        category=src["category"], subcategory=src["subcategory"], tags=", ".join(src["tags"]),
        base_image=src["base_image"], instruction=src["instruction"], tests=tests, solution=src["solution"],
        stacks=", ".join(sorted(STACKS)), task_types=", ".join(sorted(TASK_TYPES)),
    )
    card = _ask(llm, "seed_card", prompt, lambda t: _parse_card(t, name), log_dir, json_mode=True)
    card["source_dir"] = src["source_dir"]  # where the novelty gate reads the seed from
    return card


def build_cards(llm, targets: list[str] | None = None, root: Path = config.TB4_DIR) -> list[str]:
    """Write a card for every target (a task name under ``root`` or a task directory path).

    Targets without a card yet are reverse-engineered; the rest are skipped.
    Returns the seed names written.
    """
    written = []
    for target in targets or task_names(root):
        name = task_dir_of(target, root).name
        if load_card(name) is not None:
            continue
        log_dir = config.CARDS_DIR / "_log" / name
        card = reverse_engineer(llm, target, log_dir, root)
        config.CARDS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(card_path(name), card)
        written.append(name)
    return written


# --- the novelty gate ------------------------------------------------------------

def novelty_gate(task_dir: Path, card: dict, root: Path = config.TB4_DIR) -> GateResult:
    """Reject a candidate whose instruction or tests repeat too much of its public seed."""
    seed = source_text(card.get("source_dir") or root / card["seed"])
    checks = {
        "instruction.md": (task_dir / "instruction.md").read_text(errors="replace"),
        "tests/test_outputs.py": (task_dir / "tests" / "test_outputs.py").read_text(errors="replace")
        if (task_dir / "tests" / "test_outputs.py").is_file() else "",
    }
    seed_for = {"instruction.md": seed["instruction"], "tests/test_outputs.py": seed["tests"]}
    report = {}
    for path, text in checks.items():
        share, shared = overlap(text, seed_for[path])
        report[path] = {"share": round(share, 3), "shared_pairs": shared}
        limit = config.NOVELTY_LIMITS[path]
        if share > limit:
            return GateResult(
                gate="novelty", passed=False, reason=f"{path} repeats {share:.0%} of its public seed {card['seed']}",
                failure_report=(
                    f"Your {path} shares {shared} word pairs ({share:.0%}) with the public seed task "
                    f"{card['seed']}. Rewrite it in your own words: change the scenario, names, data and "
                    "file layout. Keep the skill and the mechanism, not the wording."
                ),
                details=report,
            )
    return GateResult(gate="novelty", passed=True, details=report)


# --- command line -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m generator.public_seeds")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="public tasks found and cards written")
    b = sub.add_parser("build", help="reverse-engineer cards for tasks that lack one")
    b.add_argument(
        "--only", nargs="*",
        help="task names under the Terminal-Bench 4 directory, or paths to any Harbor task directory",
    )
    args = ap.parse_args(argv)

    names = task_names()
    if args.cmd == "list":
        have = {c["seed"] for c in available_cards()}
        print(f"{len(names)} public tasks in {config.TB4_DIR}; {len(have)} cards")
        for n in names:
            print(("card    " if n in have else "no card ") + n)
        return 0
    if not names:
        print(f"no public tasks in {config.TB4_DIR}; run scripts/fetch_seeds.sh", file=sys.stderr)
        return 1
    from generator.llm import LLM

    done = build_cards(LLM(), args.only)
    print(f"wrote {len(done)} card(s): {', '.join(done) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
