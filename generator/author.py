"""Authoring: turn a spec seed into the files of a Harbor task.

The model writes the parts that need invention (scenario, environment,
solution, tests, instruction). Code writes the parts that must be exact
(task.toml, tests/test.sh, the canary line), so a formatting slip by the model
cannot break the reward contract or the schema.
"""

import json
import re
from pathlib import Path

from generator import config
from generator.llm import LLM, TruncatedReply, parse_json
from generator.similarity import max_similarity
from generator.taxonomy import STACKS, SpecSeed
from pipeline.store import atomic_write_text

# The opening marker is matched leniently: with many files the model sometimes
# mistypes its last characters (">>=" or ">>`" for ">>>"). The path is what
# matters, and it is validated separately.
FILE_BLOCK = re.compile(r"<<<FILE ([^>\n]+?)>{1,3}[^\n>]{0,2}\n(.*?)\n?<<<END>{2,3}", re.DOTALL)

# The only places the model may write. task.toml and tests/test.sh are ours.
MODEL_WRITABLE = re.compile(
    r"^(environment/[A-Za-z0-9._/\-]+|solution/solve\.sh|tests/test_outputs\.py|instruction\.md)$"
)

SPEC_REQUIRED_KEYS = (
    "slug",
    "title",
    "description",
    "scenario",
    "environment",
    "requirements",
    "pitfalls",
    "solution_outline",
)

BUILD_DIR_NAME = "_build"  # environment/_build/: generators, removed from the image (see pipeline/answer_keys.py)


# "knowable_from" must point into the container. These phrases mean it points
# at what the solver is told instead.
KNOWABLE_FROM_INSTRUCTION = re.compile(
    r"\b(instructions?|scenario|task (description|statement)|problem statement|prompt)\b"
    r"|\brequirements? (says?|states?|specif\w+|mentions?)\b|\bR\d+\b",
    re.IGNORECASE,
)

# Something inside the container: a path, a file name, quoted code, a command,
# or a verb for looking at data or running a program.
NAMES_A_CONTAINER_SOURCE = re.compile(
    r"/[\w.\-]+|`[^`]+`|\b[\w\-]+\.[A-Za-z]{1,5}\b"
    r"|\b(run(s|ning)?|ran|execut\w+|invok\w+|output|stdout|stderr|exit code|command|query\w*|inspect\w*"
    r"|examin\w+|observ\w+|compar\w+|measur\w+|read(ing)?|look(ing)? at|open(ing)?|check(ing)?"
    r"|files?|logs?|data|database|table|script|program|binary|source|code|config\w*|history|commit\w*)\b",
    re.IGNORECASE,
)


def instruction_is_the_only_source(knowable_from: str) -> bool:
    """True if a pitfall is said to be knowable from the instruction and from nothing in the container.

    Mentioning a requirement id or the scenario beside a real container source
    is fine: "R3 fixes the format; running the tool on a pre-release version
    shows it is accepted" names an experiment. Only a pitfall whose sole source
    is what the solver is told is a hint in disguise.
    """
    if not KNOWABLE_FROM_INSTRUCTION.search(knowable_from):
        return False
    remainder = KNOWABLE_FROM_INSTRUCTION.sub(" ", knowable_from)
    return not NAMES_A_CONTAINER_SOURCE.search(remainder)


class AuthoringError(Exception):
    """The model's output could not be turned into task files."""


# Hidden-reasoning budget per step. Steps whose output is executed (solution,
# tests) get more room to check their own work.
REASONING_TOKENS = {
    "pitch": 2000,
    "seed_card": 6000,
    "spec": 6000,
    "spec_review": 5000,
    "spec_revise": 6000,
    "environment": 12000,
    "tests": 12000,
    "alignment": 6000,
    "repair": 10000,
    # Auditing a failed run can mean working out the task's answer, which
    # needs more room than a short reply: at 6,000 the model returned an empty
    # object three times in a row.
    "run_analysis": 12000,
    "instruction_rewrite": 3000,
    "repair_build": 8000,
}
PARSE_ATTEMPTS = 3

# Upper limit on a whole reply, reasoning included, for a call that writes
# only part of a task. A reply that runs on (the model reasoning in the open,
# or rewriting its answer) used to cost the full 32,000 tokens and five
# minutes before it was cut off and thrown away; it now stops sooner.
MAX_TOKENS = {"repair_build": 20000}


def _ask(llm: LLM, purpose: str, prompt: str, parse, log_dir: Path, **chat_kwargs):
    """Call the model and parse its reply.

    If the reply cannot be used, ask again and say what was wrong with it, so
    the retry is not a blind repeat of the same request.
    """
    last_error: Exception | None = None
    base_prompt = prompt
    for _ in range(PARSE_ATTEMPTS):
        if last_error is not None:
            prompt = (
                f"{base_prompt}\n\nYour previous reply could not be used: {last_error}\n"
                "Reply again, complete and in exactly the required format."
            )
        try:
            if purpose in MAX_TOKENS:
                chat_kwargs.setdefault("max_tokens", MAX_TOKENS[purpose])
            result = llm.chat(
                prompt,
                purpose=purpose,
                log_dir=log_dir,
                reasoning_tokens=REASONING_TOKENS[purpose],
                **chat_kwargs,
            )
            return parse(result.text)
        except (AuthoringError, TruncatedReply, json.JSONDecodeError, AttributeError, TypeError) as exc:
            last_error = exc
    raise AuthoringError(f"{purpose} step gave unusable output {PARSE_ATTEMPTS} times: {last_error}")


def apply_design(text: str, features) -> str:
    """Keep the wording that belongs to this run's design.

    Prompt files hold alternatives in blocks that start with a line
    ``<<IF name>>``, may have a line ``<<ELSE>>``, and end with ``<<END IF>>``.
    ``name`` is a design feature with underscores (``full_reports``). Blocks
    do not nest.
    """
    active = {f.replace("-", "_") for f in features}
    out: list[str] = []
    keep = True
    inside = False
    for line in text.split("\n"):
        marker = line.strip()
        if marker.startswith("<<IF ") and marker.endswith(">>"):
            if inside:
                raise AuthoringError("prompt design blocks must not nest")
            inside, keep = True, marker[5:-2].strip() in active
        elif marker == "<<ELSE>>" and inside:
            keep = not keep
        elif marker == "<<END IF>>" and inside:
            inside, keep = False, True
        elif keep:
            out.append(line)
    if inside:
        raise AuthoringError("prompt design block was not closed")
    return "\n".join(out)


def _prompt(name: str, **fields) -> str:
    """The prompt for one call, in this run's design: the shared prompt with the design's blocks kept."""
    path = config.PROMPTS_DIR / f"{name}.md"
    return apply_design(path.read_text(), config.design()).format(**fields)


def parse_file_blocks(text: str) -> dict[str, str]:
    """Extract {relative path: content} from <<<FILE path>>> ... <<<END>>> blocks."""
    files: dict[str, str] = {}
    if text.count("<<<FILE ") != len(FILE_BLOCK.findall(text)):
        # A block without its end marker means the reply was cut off. Dropping
        # it silently would look like "no change" to that file.
        raise AuthoringError("a file block is missing its <<<END>>> marker (reply cut off?)")
    for path, content in FILE_BLOCK.findall(text):
        path = path.strip()
        if ".." in path.split("/") or not MODEL_WRITABLE.match(path):
            raise AuthoringError(f"model tried to write a disallowed path: {path!r}")
        files[path] = content.rstrip("\n") + "\n"
    if not files:
        raise AuthoringError("no file blocks found in the model's reply")
    return files


def render_files(files: dict[str, str]) -> str:
    """Show files to the model in the same block format it must reply in."""
    return "\n\n".join(
        f"<<<FILE {path}>>>\n{content.rstrip()}\n<<<END>>>" for path, content in sorted(files.items())
    )


# --- Model calls -----------------------------------------------------------


def write_pitches(llm: LLM, seed: SpecSeed, avoid: list[str], log_dir: Path) -> list[dict]:
    avoid_text = "\n".join(f"- {item}" for item in avoid) or "- (nothing yet)"
    def parse(text: str) -> list[dict]:
        pitches = parse_json(text).get("pitches") or []
        pitches = [p for p in pitches if isinstance(p, dict) and p.get("title") and p.get("summary")]
        if not pitches:
            raise AuthoringError("no usable pitches")
        return pitches

    prompt = _prompt("pitch", n_pitches=config.N_PITCHES, seed=seed.describe(), avoid=avoid_text)
    return _ask(llm, "pitch", prompt, parse, log_dir, json_mode=True, temperature=1.0)


def choose_pitch(pitches: list[dict], existing: list[str]) -> tuple[dict, float]:
    """Pick the pitch least similar to tasks that already exist."""
    scored = [
        (max_similarity(f"{p['title']} {p['summary']}", existing), index, p)
        for index, p in enumerate(pitches)
    ]
    score, _, pitch = min(scored)
    return pitch, score


def write_spec(llm: LLM, seed: SpecSeed, pitch: dict, log_dir: Path) -> dict:
    prompt = _prompt(
        "spec",
        seed=seed.describe(),
        pitch_title=pitch["title"],
        pitch_summary=pitch["summary"],
        pitch_hard=pitch.get("what_is_hard", ""),
    )
    return _ask(llm, "spec", prompt, _parse_spec, log_dir, json_mode=True)


def _parse_spec(text: str) -> dict:
    spec = parse_json(text)
    if isinstance(spec, list) and len(spec) == 1:
        spec = spec[0]  # the model sometimes wraps the object in a list
    if not isinstance(spec, dict):
        raise AuthoringError("spec reply is not a JSON object")
    missing = [key for key in SPEC_REQUIRED_KEYS if not spec.get(key)]
    if missing:
        raise AuthoringError(f"spec is missing keys: {missing}")
    # Size limits keep the task small enough for the author to get right.
    for key, (low, high) in (
        ("requirements", config.spec_requirements_range()),
        ("pitfalls", config.SPEC_PITFALLS_RANGE),
    ):
        count = len(spec[key]) if isinstance(spec[key], list) else 0
        if not low <= count <= high:
            raise AuthoringError(f"the spec has {count} {key}; it must have between {low} and {high}")
    # A pitfall that is only knowable because the instruction mentions it is a hint.
    for index, pitfall in enumerate(spec["pitfalls"], 1):
        source = str(pitfall.get("knowable_from", "")) if isinstance(pitfall, dict) else ""
        if not source.strip():
            raise AuthoringError(f"pitfall {index} has no knowable_from")
        if instruction_is_the_only_source(source):
            raise AuthoringError(
                f"pitfall {index}'s knowable_from names only the instruction ({source[:120]!r}). It must name a "
                "file, a command's output or an observable behaviour in the container; the instruction must "
                "not be what reveals a pitfall"
            )
    spec["slug"] = slugify(spec["slug"])
    return spec


def rewrite_instruction(llm: LLM, instruction: str, tests: str, findings: list[dict], log_dir: Path) -> str:
    """Ask once for an instruction without the quoted give-aways."""
    listed = "\n".join(f'- "{f["sentence"]}" ({f["why"]})' for f in findings)
    prompt = _prompt("instruction_rewrite", instruction=instruction, tests=tests, findings=listed)

    def parse(text: str) -> str:
        files = parse_file_blocks(text)
        if "instruction.md" not in files:
            raise AuthoringError("the reply did not contain instruction.md")
        return files["instruction.md"]

    return _ask(llm, "instruction_rewrite", prompt, parse, log_dir, temperature=0.4)


def review_spec(llm: LLM, seed: SpecSeed, spec: dict, log_dir: Path) -> dict:
    """Ask for an evidence-based review of the spec. Returns the raw answers."""

    def parse(text: str) -> dict:
        review = parse_json(text)
        for key in ("skills", "requirements", "pitfalls"):
            if not isinstance(review.get(key), list):
                raise AuthoringError(f"spec review is missing the {key!r} list")
        return review

    prompt = _prompt("spec_review", seed=seed.describe(), spec=json.dumps(spec, indent=2))
    return _ask(llm, "spec_review", prompt, parse, log_dir, json_mode=True, temperature=0.2)


def revise_spec(llm: LLM, seed: SpecSeed, spec: dict, problems: list[str], log_dir: Path) -> dict:
    prompt = _prompt(
        "spec_revise",
        seed=seed.describe(),
        spec=json.dumps(spec, indent=2),
        problems="\n".join(f"- {p}" for p in problems),
        req_low=config.spec_requirements_range()[0],
        req_high=config.spec_requirements_range()[1],
        pit_low=config.SPEC_PITFALLS_RANGE[0],
        pit_high=config.SPEC_PITFALLS_RANGE[1],
    )
    revised = _ask(llm, "spec_revise", prompt, _parse_spec, log_dir, json_mode=True, temperature=0.4)
    revised["slug"] = spec["slug"]  # the slug names directories; keep it stable
    return revised


def review_alignment(
    llm: LLM, instruction: str, tests: str, environment: dict[str, str], test_names: list[str], log_dir: Path
) -> dict:
    """Ask, per test, where the agent could learn what the test expects."""

    def parse(text: str) -> dict:
        review = parse_json(text)
        listed = {t.get("name") for t in review.get("tests") or [] if isinstance(t, dict)}
        missing = [name for name in test_names if name not in listed]
        if missing:
            raise AuthoringError(f"alignment review skipped tests: {missing[:5]}")
        return review

    prompt = _prompt(
        "alignment", instruction=instruction, tests=tests, environment=render_files(environment)
    )
    return _ask(llm, "alignment", prompt, parse, log_dir, json_mode=True, temperature=0.2)


def write_environment(llm: LLM, seed: SpecSeed, spec: dict, log_dir: Path) -> dict[str, str]:
    def parse(text: str) -> dict[str, str]:
        files = parse_file_blocks(text)
        for required in ("environment/Dockerfile", "solution/solve.sh"):
            if required not in files:
                raise AuthoringError(f"environment step did not write {required}")
        return files

    prompt = _prompt(
        "environment", spec=json.dumps(spec, indent=2), base_image=STACKS[seed.stack]["base_image"]
    )
    return _ask(llm, "environment", prompt, parse, log_dir, temperature=0.4)


def write_tests(llm: LLM, spec: dict, files: dict[str, str], log_dir: Path) -> dict[str, str]:
    wanted = ("tests/test_outputs.py", "instruction.md")

    def parse(text: str) -> dict[str, str]:
        new_files = parse_file_blocks(text)
        for required in wanted:
            if required not in new_files:
                raise AuthoringError(f"tests step did not write {required}")
        return {path: new_files[path] for path in wanted}

    prompt = _prompt("tests", spec=json.dumps(spec, indent=2), files=render_files(files))
    return _ask(llm, "tests", prompt, parse, log_dir, temperature=0.4)


def write_repair(llm: LLM, spec: dict, files: dict[str, str], failure: str, log_dir: Path) -> dict[str, str]:
    """Ask the model for corrected files. Returns only the files it changed."""
    old_tests = files.get("tests/test_outputs.py", "")

    def parse(text: str) -> dict[str, str]:
        changed = parse_file_blocks(text)
        new_tests = changed.get("tests/test_outputs.py")
        if new_tests is not None:
            problem = weakened_tests(old_tests, new_tests)
            if problem:
                raise AuthoringError(
                    f"the repair weakens the tests ({problem}). Fix what is wrong without removing "
                    "tests or assertions; a wrong expected value should be corrected, not deleted"
                )
        return changed

    prompt = _prompt("repair", spec=json.dumps(spec, indent=2), files=render_files(files), failure=failure)
    return _ask(llm, "repair", prompt, parse, log_dir, temperature=0.3)


def _only(files: dict[str, str], prefix: str, step: str) -> dict[str, str]:
    stray = sorted(path for path in files if not path.startswith(prefix))
    if stray:
        raise AuthoringError(f"the {step} step may only write files under {prefix}; it also wrote {stray[:5]}")
    return files


def write_build_repair(llm: LLM, spec: dict, container: dict[str, str], failure: str, log_dir: Path) -> dict[str, str]:
    """Repair an image that did not build. Shown only the container's files, and may change only those."""
    prompt = _prompt("repair_build", spec=json.dumps(spec, indent=2), files=render_files(container), failure=failure)
    return _ask(
        llm, "repair_build", prompt, lambda text: _only(parse_file_blocks(text), "environment/", "build repair"), log_dir, temperature=0.3
    )


def test_profile(source: str) -> dict:
    """Names of the test functions and how many assertions the file makes."""
    import ast

    tree = ast.parse(source)
    names = sorted(
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    )  # fmt: skip
    asserts = sum(isinstance(n, ast.Assert) for n in ast.walk(tree))
    return {"tests": names, "asserts": asserts}


MIN_ASSERT_RATIO = 0.8


def weakened_tests(old_source: str, new_source: str) -> str:
    """Why the new tests are weaker than the old ones, or "" if they are not.

    This catches the crude ways a repair can make a failing task pass: dropping
    tests or stripping assertions. It cannot tell whether a changed expected
    value is more or less correct; the alignment review and run analysis cover that.
    """
    try:
        old, new = test_profile(old_source), test_profile(new_source)
    except SyntaxError:
        return ""  # the structural gate reports syntax errors
    if len(new["tests"]) < len(old["tests"]):
        removed = sorted(set(old["tests"]) - set(new["tests"]))
        return f"{len(old['tests'])} tests became {len(new['tests'])}; removed: {removed[:6]}"
    if old["asserts"] and new["asserts"] < old["asserts"] * MIN_ASSERT_RATIO:
        return f"{old['asserts']} assertions became {new['asserts']}"
    return ""


# --- Assembly (code-owned parts) -------------------------------------------


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or "task"


def _toml(value) -> str:
    """JSON encoding is valid TOML for strings, numbers and flat lists of them."""
    return json.dumps(value)


def render_task_toml(spec: dict, seed: SpecSeed) -> str:
    expert = float(spec.get("expert_time_estimate_min") or 45)
    difficulty = spec.get("difficulty") if spec.get("difficulty") in ("medium", "hard") else "hard"
    tags = [slugify(str(t)) for t in (spec.get("tags") or [seed.domain])][:4]
    values = {
        "__NAME__": _toml(f"{config.TASK_ORG}/{spec['slug']}"),
        "__DESCRIPTION__": _toml(" ".join(str(spec["description"]).split())),
        "__TAGS__": _toml(tags),
        "__DIFFICULTY__": _toml(difficulty),
        "__CATEGORY__": _toml(slugify(str(spec.get("category") or "software-engineering"))),
        "__EXPERT_MIN__": _toml(expert),
        "__JUNIOR_MIN__": _toml(expert * 3),
        "__GENERATOR_MODEL__": _toml(config.MODEL),
        "__SEED_DOMAIN__": _toml(seed.domain),
        "__SEED_TASK_TYPE__": _toml(seed.task_type),
        "__SEED_STACK__": _toml(seed.stack),
        "__SEED_SKILLS__": _toml(seed.skills),
        "__SEED_DEPTH__": _toml(seed.depth),
        "__SEED_WIDTH__": _toml(seed.width),
        "__SEED_DIFFICULTY_SOURCE__": _toml(seed.difficulty_source),
        "__VERIFIER_TIMEOUT__": _toml(config.VERIFIER_TIMEOUT_SEC),
        "__AGENT_TIMEOUT__": _toml(config.AGENT_TIMEOUT_SEC),
        "__BUILD_TIMEOUT__": _toml(config.BUILD_TIMEOUT_SEC),
    }
    text = (config.TEMPLATES_DIR / "task.toml").read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


DOCKERFILE_FOOTER_MARKER = "# --- Verifier tooling"


def strip_code_owned(path: str, content: str) -> str:
    """Remove what code appended, so the model only ever sees and edits its own part."""
    if path == "environment/Dockerfile" and DOCKERFILE_FOOTER_MARKER in content:
        content = content.split(DOCKERFILE_FOOTER_MARKER, 1)[0].rstrip() + "\n"
    return content


def _finish_file(path: str, content: str) -> str:
    """Canary line on model-written files, plus the verifier footer on the Dockerfile."""
    content = _stamp_canary(path, strip_code_owned(path, content))
    if path == "environment/Dockerfile":
        content = content.rstrip() + "\n\n" + (config.TEMPLATES_DIR / "Dockerfile.footer").read_text()
    return content


def _stamp_canary(path: str, content: str) -> str:
    """Add the canary comment to a model-written file (not to the instruction)."""
    if "terminal-bench-canary" in content:
        return content
    comment = f"# {config.CANARY_LINE}\n"
    if path == "solution/solve.sh":
        if not content.startswith("#!"):
            content = "#!/bin/bash\n" + content
        shebang, rest = content.split("\n", 1)
        return f"{shebang}\n{comment}{rest}"
    if path in ("environment/Dockerfile", "tests/test_outputs.py"):
        return comment + content
    return content


def assemble_task(task_dir: Path, spec: dict, seed: SpecSeed, files: dict[str, str]) -> None:
    """Write a complete Harbor task directory from the model's files."""
    for path, content in files.items():
        target = task_dir / path
        atomic_write_text(target, _finish_file(path, content))
        if path.endswith(".sh"):
            target.chmod(0o755)
    atomic_write_text(task_dir / "task.toml", render_task_toml(spec, seed))
    for name in ("test.sh", "conftest.py"):
        text = (config.TEMPLATES_DIR / name).read_text().replace("__CANARY__", config.CANARY_LINE)
        atomic_write_text(task_dir / "tests" / name, text)
    (task_dir / "tests" / "test.sh").chmod(0o755)
