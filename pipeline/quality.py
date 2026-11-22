"""Coherence checks that the oracle and nop runs cannot make.

Checks that use no model:

- ``coverage_check``: every requirement id is named by a test and every test
  names a requirement. Advisory: recorded with the gate results, never a
  reason to reject or repair.
- ``instruction_giveaways``: sentences of the instruction that hand over part
  of the solution. A finding gets the instruction rewritten once.
- ``environment_giveaways``: the same test over comments and documents in the
  container. Advisory.

Verdicts computed from a model's structured evidence:

- ``judge_spec_review``: on a spec, before any file is written. Advisory: its
  problems go into at most one revision of the spec, and the task proceeds
  either way.
- ``judge_alignment``: on the finished task, after the band measurement. A
  test is fair if what it checks is stated in the instruction (the quoted
  sentence is verified to be there) or can be found in the container (the
  named path is verified to be there). It raises flags; it never stops a
  task from shipping.

The model supplies evidence in structured form; the verdict is computed here
from that evidence, so a reviewer cannot simply declare a task acceptable.
"""

import ast
import re

from pipeline.gates import GateResult

REQUIREMENT_ID = re.compile(r"\bR\d+\b")
MIN_SKILLS_NEEDED_FRACTION = 0.6
MIN_QUOTE_WORDS = 3


def find_test_functions(tests_source: str) -> list[tuple[str, str]]:
    """(name, docstring) for every test function, including methods of classes."""
    tree = ast.parse(tests_source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            found.append((node.name, ast.get_docstring(node) or ""))
    return found


# --- Coverage (deterministic) ----------------------------------------------


def coverage_check(spec: dict, tests_source: str) -> GateResult:
    required = [str(r.get("id")) for r in spec.get("requirements", []) if isinstance(r, dict)]
    try:
        tests = find_test_functions(tests_source)
    except SyntaxError as exc:
        return GateResult("coverage", False, "tests do not parse", f"tests/test_outputs.py has a syntax error: {exc}")

    covered: set[str] = set()
    unlabelled, unknown = [], []
    for name, doc in tests:
        ids = REQUIREMENT_ID.findall(doc.split("\n\n")[0])
        if not ids:
            unlabelled.append(name)
        for rid in ids:
            if rid in required:
                covered.add(rid)
            else:
                unknown.append(f"{name} -> {rid}")
    uncovered = [rid for rid in required if rid not in covered]

    problems = []
    if uncovered:
        problems.append(f"Requirements with no test: {', '.join(uncovered)}. Add a test for each.")
    if unlabelled:
        problems.append(
            "Tests whose docstring does not begin with a requirement id: "
            + ", ".join(unlabelled[:10])
            + ". Each test must check a stated requirement and name it."
        )
    if unknown:
        problems.append("Tests naming requirement ids that do not exist: " + ", ".join(unknown[:10]))

    passed = not problems
    return GateResult(
        gate="coverage",
        passed=passed,
        reason="ok" if passed else f"{len(uncovered)} uncovered requirement(s), {len(unlabelled)} unlabelled test(s)",
        failure_report="" if passed else "Requirement coverage check failed:\n" + "\n".join(f"- {p}" for p in problems),
        details={"requirements": required, "covered": sorted(covered), "uncovered": uncovered, "unlabelled": unlabelled, "n_tests": len(tests)},
    )


# --- Instruction give-aways (deterministic) ---------------------------------

# Phrases that offer advice, name a cause, or point at where the answer lives.
HINT_PHRASES = [
    r"one thing i'?ve noticed", r"\bbe careful\b", r"\bmake sure to\b", r"\bkeep in mind\b", r"\bwatch out\b",
    r"\bdon'?t forget\b", r"\bpay attention\b", r"\bhint\b", r"\bi suspect\b", r"\bmy guess\b",
    r"\byou (may|might) want to\b", r"\byou should (check|look|inspect|verify|start)\b", r"\btake a look at\b",
    r"\b(inspect|check|read|look at|review)\b.{0,60}\bcarefully\b", r"\b(probably|likely) (because|due to|caused)\b", r"\bmight be (caused|due|related)\b",
    r"\bthe (bug|problem|issue|defect|culprit) is\b", r"\broot cause\b",
]
LOCATION_PHRASES = [
    r"\b(is|are) documented (in|at)\b", r"\bdocument(ation|ed)? (lives?|is|are) (in|at|under)\b", r"\blives? (in|at|under)\b",
    r"\b(is|are) described in\b", r"\b(is|are) defined in\b", r"\b(can|could) be found (in|at|under)\b",
    r"\brefer to\b", r"\bconsult\b", r"\bsee (the )?[`/]",
]
COUNT_PHRASE = r"\b(two|three|four|five|six|\d+|several|multiple|a couple of|a few) (separate |distinct |different |known )?(bugs?|defects?|issues?|problems?|things? (are|is) (wrong|broken))\b"
PITFALL_OVERLAP_MIN = 0.3  # share of the smaller text's word bigrams that are shared
PITFALL_SHARED_MIN = 3  # and at least this many shared bigrams


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.split()) >= 3]


def instruction_giveaways(instruction: str, spec: dict) -> dict:
    """Sentences of the instruction that hand over part of the solution.

    Three checks, none using a model: wording shared with a pitfall's private
    description, hint or advice phrases, and pointers to where documentation
    or configuration lives or to how many things are wrong.
    """
    from generator.similarity import overlap

    pitfalls = [p.get("what", "") for p in spec.get("pitfalls") or [] if isinstance(p, dict)]
    findings = []
    for sentence in _sentences(instruction):
        lowered = sentence.lower()
        for index, what in enumerate(pitfalls, 1):
            share, shared = overlap(sentence, what)
            if share >= PITFALL_OVERLAP_MIN and shared >= PITFALL_SHARED_MIN:
                findings.append({"kind": "pitfall", "sentence": sentence, "why": f"restates hidden pitfall {index}", "score": round(share, 2)})
                break
        else:
            for kind, patterns, why in (
                ("hint", HINT_PHRASES, "gives advice or a hint"),
                ("location", LOCATION_PHRASES, "says where the answer can be found"),
                ("count", [COUNT_PHRASE], "says how many things are wrong"),
            ):
                if any(re.search(pattern, lowered) for pattern in patterns):
                    findings.append({"kind": kind, "sentence": sentence, "why": why})
                    break
    return {"tripped": bool(findings), "findings": findings, "words": len(instruction.split())}


DOCUMENT_EXTENSIONS = (".md", ".txt", ".rst", ".adoc")
LINE_COMMENT = re.compile(r"(?:^|\s)(?:#|//|--|;;)\s?(.*)$")
BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/|\"\"\"(.*?)\"\"\"", re.DOTALL)


def _prose_of(path: str, content: str) -> str:
    """The human-readable text of an environment file: all of a document, the comments of anything else."""
    if path.lower().endswith(DOCUMENT_EXTENSIONS) or path.rsplit("/", 1)[-1].upper().startswith(("README", "CHANGELOG", "NOTES")):
        return content
    pieces = [m.group(1) or m.group(2) or "" for m in BLOCK_COMMENT.finditer(content)]
    for line in content.splitlines():
        if line.lstrip().startswith("#!"):
            continue
        match = LINE_COMMENT.search(line)
        if match:
            pieces.append(match.group(1))
    return "\n".join(pieces)


def environment_giveaways(environment_files: dict[str, str], spec: dict) -> dict:
    """Comments and documents in the container that state what a pitfall is about.

    The same wording test as ``instruction_giveaways``, run over the prose of
    the environment: a comment that spells out the rule the solver is meant to
    work out gives the task away as surely as the instruction would. This is
    advisory: it is recorded with the gate results and never rejects a task or
    triggers a repair. Files under ``_build/`` are skipped; they are removed
    from the image.
    """
    from generator.similarity import overlap

    pitfalls = [p.get("what", "") for p in spec.get("pitfalls") or [] if isinstance(p, dict)]
    findings = []
    for path, content in sorted(environment_files.items()):
        relative = path.removeprefix("environment/")
        if relative == "Dockerfile" or relative.split("/")[0] == "_build":
            continue
        for sentence in _sentences(_prose_of(path, content)):
            for index, what in enumerate(pitfalls, 1):
                share, shared = overlap(sentence, what)
                if share >= PITFALL_OVERLAP_MIN and shared >= PITFALL_SHARED_MIN:
                    findings.append({"file": path, "sentence": sentence[:300], "pitfall": index, "score": round(share, 2)})
                    break
    return {"tripped": bool(findings), "findings": findings}


# --- Spec review -------------------------------------------------------------


def judge_spec_review(spec: dict, n_skills: int, review: dict) -> GateResult:
    """Compute the verdict on a spec from the reviewer's structured evidence."""
    problems: list[str] = []
    valid_ids = {str(r.get("id")) for r in spec.get("requirements", []) if isinstance(r, dict)}

    # A skill only counts if the reviewer tied it to requirements that exist.
    needed = [
        s for s in review.get("skills", [])
        if isinstance(s, dict) and s.get("needed") and set(map(str, s.get("requirement_ids") or [])) & valid_ids
    ]  # fmt: skip
    if n_skills and len(needed) / n_skills < MIN_SKILLS_NEEDED_FRACTION:
        unused = [s.get("skill") for s in review.get("skills", []) if isinstance(s, dict) and s not in needed]
        problems.append(f"Only {len(needed)} of {n_skills} sampled skills are genuinely required. Not required: {unused}")

    for item in review.get("requirements", []):
        if not isinstance(item, dict):
            continue
        for flag, meaning in (
            ("behavioural", "prescribes an implementation instead of observable behaviour"),
            ("testable", "cannot be decided from the final container state"),
            ("deterministic", "depends on timing or something that varies between runs"),
        ):
            if item.get(flag) is False:
                problems.append(f"{item.get('id')}: {meaning}. {item.get('issue') or ''}".strip())

    for item in review.get("contradictions") or []:
        if isinstance(item, dict):
            problems.append(f"Contradiction between {item.get('ids')}: {item.get('problem')}")

    for item in review.get("pitfalls", []):
        if isinstance(item, dict) and item.get("knowable") is False:
            problems.append(f"Pitfall {item.get('index')} is not knowable to the solver: {item.get('why')}")

    if review.get("pitfalls_tied_to_requirements") is False:
        problems.append("A pitfall names no requirement where it would really show.")

    if review.get("gives_away_solution") is True:
        problems.append("The scenario or requirements tell the solver what the defects are or how to fix them.")
    if review.get("realistic") is False:
        problems.append("The scenario is contrived or is a list of unrelated chores.")

    # The reviewer's own free-text list adds detail for the revision.
    extra = [str(p) for p in review.get("problems") or [] if str(p).strip()]
    passed = not problems
    return GateResult(
        gate="spec_review",
        passed=passed,
        reason="ok" if passed else f"{len(problems)} spec problem(s)",
        failure_report="\n".join(dict.fromkeys(problems + (extra if problems else []))),
        details={"problems": problems, "reviewer_notes": extra, "skills_needed": len(needed), "depth_chain": review.get("depth_chain")},
    )


# --- Alignment ---------------------------------------------------------------


def _normalise(text: str) -> str:
    """Lower-case, drop Markdown punctuation and quotation marks, and collapse whitespace.

    Quotation marks go because a quotation of a sentence that itself contains
    quoted words comes back with them changed ("type error" becomes 'type
    error' or `type error`), and that is not a different sentence.
    """
    return re.sub(r"\s+", " ", re.sub(r"[`*_#>|\\\"'\u2018\u2019\u201c\u201d]", "", text.lower())).strip()


def _quote_sentences(quote: str) -> list[str]:
    """The sentences of a quotation that are long enough to check, normalised."""
    parts = re.split(r"(?<=[.;!?])\s+|\s*(?:\.\.\.|\u2026)\s*", quote)
    parts = [_normalise(part).strip(" .,:;") for part in parts]
    return [part for part in parts if len(part.split()) >= MIN_QUOTE_WORDS]


def _quote_in_text(quote: str, text: str) -> bool:
    """True if the quotation is in the text: as one run of words, or sentence by sentence.

    A reviewer backing one test with two sentences of the instruction gives
    them as one quotation even when other text lies between them.
    Every sentence long enough to check must then be found; none may be invented.
    """
    q = _normalise(quote).strip(" .,:;")
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False
    haystack = _normalise(text)
    if q in haystack:
        return True
    sentences = _quote_sentences(quote)
    return len(sentences) >= 2 and all(sentence in haystack for sentence in sentences)


def quote_in_instruction(quote: str, instruction: str) -> bool:
    """True if the quote really appears in the instruction (not invented)."""
    return _quote_in_text(quote, instruction)


def _in_image(path: str) -> bool:
    """False for build-context files the solver never sees: the Dockerfile and everything under _build/."""
    parts = path.split("/")
    return "_build" not in parts and parts[-1] != "Dockerfile"


def quote_in_container(quote: str, environment: dict[str, str]) -> str | None:
    """The shipped file that contains the quote, or None.

    A reviewer sometimes backs a test with a sentence copied from a document
    in the container instead of from the instruction. That is evidence of the
    same kind (the expectation is written down where the solver can read it),
    so it is verified the same way.
    """
    for path, content in environment.items():
        if _in_image(path) and _quote_in_text(quote, content):
            return path
    return None


def _path_in_environment(path: str, environment: dict[str, str]) -> bool:
    """True if some distinctive part of the path appears in the task's environment.

    The reviewer names a container path; we only hold the build context. A
    path counts as real if one of its components (not a generic one such as
    "app") is the name of a shipped file or appears in the Dockerfile or the
    scripts that build the image.
    """
    if "_build" in re.split(r"[/\s]+", path):
        return False  # generators are removed from the image: the solver cannot look there
    haystack = "\n".join(list(environment) + list(environment.values())).lower()
    parts = [p for p in re.split(r"[/\s]+", path.lower()) if len(p) >= 3 and p not in ("app", "usr", "etc", "opt", "var", "tmp", "home", "root")]
    return any(part in haystack for part in parts)


def judge_alignment(instruction: str, tests_source: str, environment: dict[str, str], review: dict) -> GateResult:
    """Label the task fair or unfair from the reviewer's per-test evidence.

    A test is fair if the reviewer says so. Where its quote or path is found
    (the instruction, a file shipped in the container, a path in the
    environment, never one under _build/) is recorded, not required. This runs
    after the band measurement and never sends a task back for repair; what it
    finds becomes flags (pipeline/flags.py), not a verdict.
    """
    by_name = {t.get("name"): t for t in review.get("tests") or [] if isinstance(t, dict)}
    problems: list[str] = []
    rows = []
    for name, _doc in find_test_functions(tests_source):
        entry = by_name.get(name) or {}
        quote = str(entry.get("quote") or "")
        source = str(entry.get("discoverable_from") or "")
        quote_ok = quote_in_instruction(quote, instruction)
        quoted_file = None if quote_ok else quote_in_container(quote, environment)
        source_ok = (bool(source) and _path_in_environment(source, environment)) or bool(quoted_file)
        row = {
            "test": name,
            "checks": entry.get("checks"),
            "quote": quote,
            "quote_verified": quote_ok,
            "discoverable_from": source,
            "source_verified": source_ok,
            "quote_found_in": "instruction" if quote_ok else quoted_file,
            "fair": bool(entry.get("fair")),
            "inspects_implementation": bool(entry.get("inspects_implementation")),
            "issue": entry.get("issue") or "",
        }
        rows.append(row)
        if row["inspects_implementation"]:
            problems.append(f"{name} inspects how the solution is written instead of what it does. {row['issue']}".strip())
        elif not row["fair"]:
            problems.append(f"{name} expects something neither stated nor discoverable: {row['issue'] or row['checks']}")
        # A test the reviewer calls fair is fair. It used to need a quotation or a
        # path that code could verify as well; every in-band task rejected for
        # lacking one that was then read by hand had a fair test (three earlier cases).
        # The requirement was dropped.

    passed = not problems
    return GateResult(
        gate="alignment",
        passed=passed,
        reason="fair" if passed else f"{len(problems)} unfair test(s)",
        failure_report="" if passed else "\n".join(f"- {p}" for p in problems),
        details={"tests": rows, "problems": problems, "gives_away_solution": bool(review.get("gives_away_solution"))},
    )
