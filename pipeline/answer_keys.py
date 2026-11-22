"""Checks that a task's container does not ship its own answers.

Two ways a container has given the answer away:

- A build-time generator is left in the image. The script that made the
  starting data shows how it was made, and often carries the correct algorithm
  or a list of the defects that were planted.
- A file holds the expected result (an expected-output, reference, golden or
  answer file), sometimes named in the instruction.

Both were found in tasks that had been measured, and the transcripts showed
agents opening those files.

These checks work on file names and on the Dockerfile's text. They are a
heuristic: a generator or an answer file under an ordinary name is not caught.
"""

import re
from pathlib import Path

from generator import author

BUILD_DIR = "_build"
BUILD_DEST = "/tmp/_build"

# Names that mark a script as something that generates starting state.
GENERATOR_NAME = re.compile(
    r"^(gen([_\-.]|\d|$)|generat|seed|make_?data|setup_?data|init[_\-]|create[_\-]|populate|prepare_?data|mkdata|build_?data)",
    re.IGNORECASE,
)

# Words in a data or document file's name that mark it as holding the expected result.
ANSWER_WORDS = {"expected", "golden", "answer", "answers"}

# "reference" is an ordinary word (reference_rates.csv, api-reference.md,
# cross_reference tables). It marks an answer only together with one of these
# words in the path, or when the instruction tells the solver to match or
# compare against the file.
RESULT_WORDS = {"output", "outputs", "result", "results", "report", "reports", "solution", "solutions"}
MATCH_VERBS = re.compile(r"\b(match(es|ing)?|compar\w+|identical|same as|diff(er)?s?|agree\w*|reproduce\w*)\b", re.IGNORECASE)

# Source code is never an answer file by its name: dns_answer.go,
# golden_ratio.py and reference_counter.py are programs, not results.
SOURCE_EXTENSIONS = {
    ".py", ".go", ".js", ".mjs", ".cjs", ".ts", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".java", ".kt", ".rb",
    ".sh", ".bash", ".pl", ".php", ".lua", ".sql", ".mk", ".cmake", ".toml", ".mod", ".sum",
}
ORDINARY_COMPONENTS = {"references", "api-reference", "reference-manual", "quick-reference"}

# A generator can also write the answer at build time, so the file never
# appears among the environment files. Data-file names inside the Dockerfile
# and the generators are checked too.
DATA_FILE_IN_TEXT = re.compile(r"[\w.\-/]*[\w\-]+\.(?:csv|tsv|json|jsonl|txt|out|dat|ya?ml|xml|db|sqlite|bin|log)\b", re.IGNORECASE)

# A mention in prose counts as a file only if it starts with "/" or ends in a
# file extension. "expected/actual columns" is neither.
PATH_IN_TEXT = re.compile(r"/[\w.\-]+(?:/[\w.\-]+)*|(?:[\w.\-]+/)*[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b")


def dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with line continuations joined and comments dropped."""
    joined = re.sub(r"\\\s*\n", " ", text)
    pairs = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        pairs.append((keyword.upper(), rest.strip()))
    return pairs


def _name_tokens(component: str) -> set[str]:
    """The words of a file or directory name, lower-cased (splitting on punctuation and camelCase)."""
    stem = component.rsplit(".", 1)[0] if "." in component.strip(".") else component
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", stem) if t}


def names_an_answer(path: str, instruction: str = "") -> str | None:
    """The word that marks ``path`` as an expected-result file, or None.

    ``instruction`` lets "reference" count when the solver is told to match or
    compare against that very file.
    """
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    name = parts[-1]
    if Path(name).suffix.lower() in SOURCE_EXTENSIONS:
        return None
    tokens: set[str] = set()
    for component in parts:
        if component.lower() not in ORDINARY_COMPONENTS:
            tokens |= _name_tokens(component)
    for word in sorted(tokens & ANSWER_WORDS):
        return word
    if "reference" in _name_tokens(name):
        if tokens & RESULT_WORDS:
            return "reference"
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", instruction):
            if name in sentence and MATCH_VERBS.search(sentence):
                return "reference"
    return None


def answer_key_findings(task_dir: Path) -> list[dict]:
    """Every way this task's container gives the answer away, as {rule, path, message}."""
    task_dir = Path(task_dir)
    env_dir = task_dir / "environment"
    findings: list[dict] = []
    if not env_dir.is_dir():
        return findings

    files = sorted(p.relative_to(env_dir).as_posix() for p in env_dir.rglob("*") if p.is_file())
    in_build = [f for f in files if f.split("/")[0] == BUILD_DIR]
    dockerfile_path = env_dir / "Dockerfile"
    dockerfile = author.strip_code_owned("environment/Dockerfile", dockerfile_path.read_text()) if dockerfile_path.exists() else ""
    instructions = dockerfile_instructions(dockerfile)
    run_lines = [args for keyword, args in instructions if keyword == "RUN"]
    run_text = "\n".join(run_lines)

    instruction_path = task_dir / "instruction.md"
    instruction = instruction_path.read_text() if instruction_path.exists() else ""
    tests_path = task_dir / "tests" / "test_outputs.py"
    subject_matter = instruction + "\n" + (tests_path.read_text() if tests_path.exists() else "")

    # 1. A generator that is not under _build/. Code can only remove what is there.
    for path in files:
        if path == "Dockerfile" or path.split("/")[0] == BUILD_DIR:
            continue
        name = path.rsplit("/", 1)[-1]
        if not GENERATOR_NAME.match(name) or not re.search(rf"(?<![\w.\-]){re.escape(name)}(?![\w\-])", run_text):
            continue
        if re.search(rf"(?<![\w.\-]){re.escape(name)}(?![\w\-])", subject_matter):
            continue  # the instruction or the tests name it: it is what the solver works on, not a generator
        parent = path.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in path else ""
        removed = any(
            re.search(r"\brm\b", line) and (name in line or (parent and re.search(rf"(?<![\w.\-]){re.escape(parent)}(?![\w.\-])", line)))
            for line in run_lines
        )
        findings.append(
            {
                "rule": "generator_outside_build_dir",
                "path": f"environment/{path}",
                "left_in_image": not removed,
                "message": (
                    f"environment/{path} is run at build time and looks like a generator of starting data, but it is "
                    f"not under environment/{BUILD_DIR}/"
                    + ("" if removed else ", and it is left in the image where the solver can read it")
                    + f". Move it to environment/{BUILD_DIR}/, copy it with `COPY {BUILD_DIR}/ {BUILD_DEST}/`, run it "
                    f"from {BUILD_DEST}/, and do not delete it yourself: the pipeline removes {BUILD_DEST}."
                ),
            }
        )

    # 2. _build/ copied anywhere but /tmp/_build (including by copying everything).
    for keyword, args in instructions:
        if keyword not in ("COPY", "ADD"):
            continue
        tokens = [t for t in args.split() if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        sources, dest = tokens[:-1], tokens[-1].rstrip("/")
        for source in sources:
            clean = source.lstrip("./").rstrip("/*")
            copies_build = clean == BUILD_DIR or clean.startswith(BUILD_DIR + "/")
            copies_everything = source.rstrip("/") in (".", "", "*") or source in ("./", "./*")
            if copies_build and not (dest == BUILD_DEST or dest.startswith(BUILD_DEST + "/")):
                findings.append(
                    {
                        "rule": "build_dir_copied_elsewhere",
                        "path": "environment/Dockerfile",
                        "message": f"`{keyword} {args}` copies {BUILD_DIR}/ to {dest}. It may only be copied to {BUILD_DEST}/, which the pipeline removes from the image.",
                    }
                )
            elif copies_everything and in_build:
                findings.append(
                    {
                        "rule": "build_dir_copied_elsewhere",
                        "path": "environment/Dockerfile",
                        "message": (
                            f"`{keyword} {args}` copies the whole build context, including {BUILD_DIR}/, to {dest}, where "
                            f"it stays in the image. Copy the task's files and directories by name, and {BUILD_DIR}/ "
                            f"only with `COPY {BUILD_DIR}/ {BUILD_DEST}/`."
                        ),
                    }
                )

    # 3. A _build script run from somewhere the footer does not remove.
    for line in run_lines:
        for token in re.findall(r"[^\s;&|\"'()=]+", line):
            if BUILD_DIR not in token.split("/"):
                continue  # "debug_build" or "rebuild_index" is not the _build directory
            if not token.startswith(BUILD_DEST):
                findings.append(
                    {
                        "rule": "build_script_run_outside_tmp",
                        "path": "environment/Dockerfile",
                        "message": f"`RUN {line[:120]}` uses {token}. Scripts from {BUILD_DIR}/ must be run from {BUILD_DEST}/, the only copy the pipeline removes.",
                    }
                )

    # 4. A file that holds the expected result.
    for path in files:
        if path.split("/")[0] == BUILD_DIR:
            continue  # removed from the image; what it must not write is covered by the prompt
        word = names_an_answer(path, instruction)
        if word:
            findings.append(
                {
                    "rule": "answer_file",
                    "path": f"environment/{path}",
                    "message": (
                        f"environment/{path} looks like a file holding the expected result (its name contains "
                        f"'{word}'). The container must not ship the task's answer. Remove it, and have the "
                        "instruction state the required behaviour; tests work expected values out themselves."
                    ),
                }
            )

    # 4b. ... or a build step that writes one. The file is then created inside
    #     the image and never shows up among the environment files.
    writers = {"Dockerfile": dockerfile}
    for path in files:
        name = path.rsplit("/", 1)[-1]
        if path.split("/")[0] == BUILD_DIR or GENERATOR_NAME.match(name):
            try:
                writers[path] = (env_dir / path).read_text()
            except UnicodeDecodeError:
                continue
    for source, text in writers.items():
        seen_in_source = set()
        for mention in DATA_FILE_IN_TEXT.findall(text):
            word = names_an_answer(mention, instruction)
            if word and mention not in seen_in_source:
                seen_in_source.add(mention)
                findings.append(
                    {
                        "rule": "answer_file_written_at_build",
                        "path": f"environment/{source}",
                        "message": (
                            f"environment/{source} writes or uses `{mention}` at build time, which looks like a file "
                            f"holding the expected result (its name contains '{word}'). A generator must not write "
                            "expected results into the image."
                        ),
                    }
                )

    # 5. An instruction that points the solver at such a file. Only a file that
    #    is in the container at the start can give anything away. One that
    #    exists nowhere in the environment, and that the instruction does not
    #    say to compare or match against, is where the solver writes its own
    #    result ("write the hash to /app/bisect_answer.txt").
    if instruction:
        seen = set()
        shipped_names = {path.rsplit("/", 1)[-1] for path in files if path.split("/")[0] != BUILD_DIR}
        build_text = "\n".join(writers.values())
        for mention in PATH_IN_TEXT.findall(instruction):
            word = names_an_answer(mention, instruction)
            name = mention.rsplit("/", 1)[-1]
            in_container = name in shipped_names or re.search(rf"(?<![\w.\-]){re.escape(name)}(?![\w\-])", build_text)
            # ... or the instruction treats it as something to check against, which only an existing file can be.
            compared = any(mention in sentence and MATCH_VERBS.search(sentence) for sentence in re.split(r"(?<=[.!?])\s+|\n+", instruction))
            if word and (in_container or compared) and mention not in seen:
                seen.add(mention)
                findings.append(
                    {
                        "rule": "instruction_names_answer_file",
                        "path": "instruction.md",
                        "message": (
                            f"instruction.md refers to `{mention}`, which looks like a file holding the expected result. "
                            "The instruction must state the required behaviour, not tell the solver to match a file."
                        ),
                    }
                )
    return findings
