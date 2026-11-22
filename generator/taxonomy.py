"""Taxonomy and sampler for task spec seeds.

Code, not the model, decides what each task is about. A spec seed fixes the
domain, task type, stack, 2-3 primitive skills, persona, depth, width and
difficulty source; the model then invents a scenario inside those limits.

Sampling is hierarchical (later choices depend on earlier ones) and
deterministic in (run_id, index), so a restarted run draws the same seeds.
There are 14 domains and no domain repeats until all have been used. Task
types are balanced, not rotated: each task takes the type its domain allows
that earlier tasks of the run have used least.

The categories are our own generic ones. They are not derived from any public
task set, and no task content is borrowed.
"""

import hashlib
import random
from dataclasses import asdict, dataclass

from generator import config

# --- Stacks ----------------------------------------------------------------
# Each stack names the pinned, Debian-based base image the Dockerfile must use.
# Debian-based is required because tests/test.sh installs its tooling with apt.
STACKS: dict[str, dict[str, str]] = {
    "python": {"label": "Python 3.12", "base_image": "python:3.12-slim-bookworm"},
    "c": {"label": "C (gcc, make)", "base_image": "gcc:13-bookworm"},
    "go": {"label": "Go 1.22", "base_image": "golang:1.22-bookworm"},
    "node": {"label": "Node.js 20", "base_image": "node:20-bookworm-slim"},
    "shell": {"label": "POSIX shell and coreutils", "base_image": "debian:bookworm-slim"},
    "sql": {"label": "SQL with SQLite 3", "base_image": "python:3.12-slim-bookworm"},
}

# There is deliberately no "optimise" task type and no performance-limit
# difficulty source. Whether a time or memory limit is met depends on the
# machine and its load, so such a requirement cannot be graded with the same
# result on every run, and an unlucky run would be an unfair failure.
TASK_TYPES: dict[str, str] = {
    "debug": "Find and fix defects in existing code or configuration that currently misbehaves.",
    "implement": "Build a missing component to a written specification inside an existing project.",
    "configure": "Set up or reconfigure a service or toolchain so it meets stated operational requirements.",
    "migrate": "Convert data, a schema, a format or an interface from an old form to a new one without losing meaning.",
    "investigate": "Work out what happened from evidence in the environment and produce a precise, checkable report.",
}

# --- Domains ---------------------------------------------------------------
# Each domain lists its own primitive skills, the stacks it suits and the task
# types that make sense for it. Keeping skills under their domain is what stops
# the sampler from producing incoherent combinations.
DOMAINS: dict[str, dict] = {
    "build-systems": {
        "label": "Build systems and compilation",
        "stacks": ["c", "go", "shell"],
        "task_types": ["debug", "configure", "migrate"],
        "neighbours": ["packaging", "version-control"],
        "skills": [
            "write Makefile rules with correct prerequisites",
            "track header dependencies so incremental builds stay correct",
            "link against a static or shared library",
            "separate debug and release build variants",
            "make a build reproducible byte for byte",
            "cross-check build outputs with checksums",
            "diagnose an undefined-symbol or duplicate-symbol link error",
            "generate source files as a build step",
        ],
    },
    "version-control": {
        "label": "Version control with git",
        "stacks": ["shell", "python"],
        "task_types": ["debug", "investigate", "migrate", "configure"],
        "neighbours": ["build-systems", "text-processing"],
        "skills": [
            "bisect history to find the commit that introduced a regression",
            "rewrite history to remove or split commits",
            "recover work from the reflog or dangling objects",
            "resolve a merge conflict preserving both intents",
            "write a server-side or client-side hook",
            "inspect object storage with plumbing commands",
            "reconcile diverged branches with rebase or cherry-pick",
            "audit authorship and timestamps across history",
        ],
    },
    "databases": {
        "label": "Relational data and SQL",
        "stacks": ["sql", "python"],
        "task_types": ["debug", "migrate", "investigate", "implement"],
        "neighbours": ["data-pipelines", "text-processing"],
        "skills": [
            "write a schema migration that preserves existing rows",
            "add an index that changes a query plan",
            "express a query with window functions",
            "enforce integrity with constraints and triggers",
            "deduplicate rows under a defined tie-break rule",
            "reconcile two tables and report the differences",
            "use a recursive common table expression",
            "make a multi-statement change atomic with transactions",
        ],
    },
    "networking": {
        "label": "Network services and protocols",
        "stacks": ["python", "go", "node", "c"],
        "task_types": ["debug", "implement", "configure", "investigate"],
        "neighbours": ["process-management", "security"],
        "skills": [
            "implement a length-prefixed or line-delimited wire protocol",
            "handle partial reads and message framing on a stream socket",
            "apply timeouts and bounded retries with backoff",
            "serve several clients concurrently without blocking",
            "parse and validate an HTTP request by hand",
            "bind and route by hostname or path on localhost",
            "diagnose a connection failure from socket state",
            "shut a server down gracefully, finishing in-flight work",
        ],
    },
    "file-formats": {
        "label": "Binary and archive file formats",
        "stacks": ["python", "c", "go"],
        "task_types": ["debug", "implement", "migrate", "investigate"],
        "neighbours": ["text-processing", "security"],
        "skills": [
            "parse a binary header with fixed and variable-length fields",
            "handle byte order and alignment correctly",
            "verify and compute a checksum such as CRC32",
            "read and write a chunked or tagged container layout",
            "recover readable records from a partly corrupted file",
            "stream a large file without loading it into memory",
            "round-trip a format so output equals input byte for byte",
            "decode a run-length or delta encoding",
        ],
    },
    "security": {
        "label": "Security hardening and cryptographic tooling",
        "stacks": ["python", "shell", "c", "node"],
        "task_types": ["debug", "configure", "investigate", "implement"],
        "neighbours": ["networking", "process-management"],
        "skills": [
            "validate untrusted input against path traversal",
            "set file ownership and permission bits to least privilege",
            "verify a signature or message authentication code",
            "compare secrets in constant time",
            "build and validate a certificate chain",
            "find and remove a hard-coded credential",
            "derive a key from a password with a stated work factor",
            "audit a log for evidence of misuse",
        ],
    },
    "numerical": {
        "label": "Numerical and scientific computing",
        "stacks": ["python", "c", "go"],
        "task_types": ["debug", "implement", "investigate"],
        "neighbours": ["data-pipelines", "file-formats"],
        "skills": [
            "control floating-point error with a stable summation or formulation",
            "implement fixed-point or decimal arithmetic with a rounding rule",
            "interpolate or resample an unevenly spaced series",
            "solve a small linear system and check the residual",
            "compute streaming statistics in a single pass",
            "convert between units with exact rational factors",
            "integrate a simple differential equation with a fixed step",
            "detect and handle overflow and special values",
        ],
    },
    "process-management": {
        "label": "Processes, signals and job control",
        "stacks": ["shell", "python", "c", "go"],
        "task_types": ["debug", "implement", "configure", "investigate"],
        "neighbours": ["networking", "scheduling"],
        "skills": [
            "handle termination signals and clean up child processes",
            "supervise a worker and restart it under a backoff policy",
            "coordinate processes with a lock file or advisory lock",
            "connect processes with pipes without deadlocking",
            "enforce a time or memory limit on a child process",
            "reap zombie processes correctly",
            "daemonise and write a pid file safely",
            "propagate exit codes through a pipeline",
        ],
    },
    "packaging": {
        "label": "Packaging and dependency management",
        "stacks": ["python", "node", "shell", "go"],
        "task_types": ["debug", "configure", "migrate", "implement"],
        "neighbours": ["build-systems", "version-control"],
        "skills": [
            "resolve a version conflict between dependencies",
            "build an installable package with an entry point",
            "pin and verify dependencies with hashes",
            "vendor a dependency for offline installation",
            "ship data files inside a package and load them at run time",
            "compare versions under a stated precedence rule",
            "split one package into several with a shared namespace",
            "write an install script that is safe to run twice",
        ],
    },
    "text-processing": {
        "label": "Text, encodings and parsing",
        "stacks": ["python", "shell", "go", "node"],
        "task_types": ["debug", "implement", "migrate", "investigate"],
        "neighbours": ["file-formats", "data-pipelines"],
        "skills": [
            "detect and convert between text encodings",
            "normalise Unicode before comparing or sorting",
            "write a tokenizer for a small grammar",
            "parse quoted and escaped fields in a delimited format",
            "apply a multi-line pattern across record boundaries",
            "sort with a locale-independent collation rule",
            "produce a unified diff between two texts",
            "wrap and align text by display width",
        ],
    },
    "scheduling": {
        "label": "Time, calendars and scheduling",
        "stacks": ["python", "go", "node", "shell"],
        "task_types": ["debug", "implement", "investigate", "migrate"],
        "neighbours": ["process-management", "data-pipelines"],
        "skills": [
            "convert timestamps across time zones including daylight-saving changes",
            "expand a recurrence rule into concrete occurrences",
            "evaluate a cron-style expression",
            "order jobs under dependency constraints",
            "merge and query overlapping intervals",
            "compute business-day offsets with a holiday list",
            "parse durations and timestamps in mixed formats",
            "detect double-booking under a capacity limit",
        ],
    },
    "interpreters": {
        "label": "Interpreters, compilers and small languages",
        "stacks": ["python", "c", "go", "node"],
        "task_types": ["debug", "implement", "migrate"],
        "neighbours": ["text-processing", "numerical"],
        "skills": [
            "parse expressions with operator precedence and associativity",
            "evaluate with lexical scoping and closures",
            "implement a stack-based bytecode instruction set",
            "report errors with accurate line and column positions",
            "fold constants or eliminate dead code safely",
            "expand macros or templates without capturing names",
            "type-check a small language and reject ill-typed programs",
            "serialise and reload a compiled program",
        ],
    },
    "data-pipelines": {
        "label": "Data transformation pipelines",
        "stacks": ["python", "shell", "go", "sql"],
        "task_types": ["debug", "implement", "migrate", "investigate"],
        "neighbours": ["databases", "text-processing"],
        "skills": [
            "join two datasets on a normalised key",
            "aggregate records into time or value buckets",
            "handle late, duplicate and out-of-order records",
            "validate rows against a schema and quarantine rejects",
            "make a batch job idempotent with checkpoints",
            "merge sorted inputs larger than memory",
            "track provenance from each output row to its inputs",
            "reconcile totals between stages",
        ],
    },
    "storage": {
        "label": "File systems and storage layout",
        "stacks": ["shell", "python", "c", "go"],
        "task_types": ["debug", "implement", "configure", "investigate", "migrate"],
        "neighbours": ["file-formats", "process-management"],
        "skills": [
            "handle hard links and symbolic links without following cycles",
            "replace a file atomically so readers never see a partial write",
            "deduplicate files by content hash",
            "preserve timestamps, modes and sparse regions when copying",
            "apply a retention and rotation policy to a directory tree",
            "walk a tree in a deterministic order",
            "account for disk usage without double counting links",
            "detect and repair an inconsistent index of files",
        ],
    },
}

PERSONAS: list[str] = [
    "an on-call engineer in the middle of an incident who needs the system working again",
    "a researcher trying to reproduce a result from someone else's tooling",
    "a new maintainer who has inherited an undocumented legacy tool",
    "a release manager who must unblock a release before a deadline",
    "a data analyst whose weekly report has started giving wrong numbers",
    "a platform engineer consolidating several teams' ad-hoc scripts",
    "an open-source maintainer responding to a detailed bug report",
    "an auditor who must produce evidence that can be checked independently",
]

DIFFICULTY_SOURCES: dict[str, str] = {
    "interacting-bugs": "Several defects interact, so fixing one exposes or masks another. Each defect alone looks plausible, and the symptoms the persona observes do not map one-to-one onto the defects.",
    "discover-in-environment": "Key facts are not in the instruction and must be found by investigating the container: files, data, history, tool output. There are several plausible places to look, and the instruction does not say which.",
    "needs-experiment": "The correct behaviour can only be established by running something in the container and observing it: matching what an existing tool actually does, measuring a property of the data, or reproducing a failure. Reading alone is not enough, and what the documentation or the names suggest is not quite what happens.",
    "stated-edge-cases": "The required behaviour has precise rules whose edge cases are easy to get wrong (boundaries, ties, empty and malformed input).",
}

# How often each source is drawn. "stated-edge-cases" is kept rare: it tends to
# produce rule lists, which the evaluator reads and follows without difficulty.
DIFFICULTY_WEIGHTS: dict[str, int] = {
    "interacting-bugs": 3,
    "discover-in-environment": 3,
    "needs-experiment": 3,
    "stated-edge-cases": 1,
}

DEPTH_RANGE = (2, 5)  # steps where each depends on the result of the one before
WIDTH_RANGE = (1, 4)  # independent sub-problems that must all be solved
SKILLS_RANGE = (2, 3)


@dataclass
class SpecSeed:
    """Everything code decides about a task before the model is called."""

    index: int
    domain: str
    task_type: str
    stack: str
    skills: list[str]
    persona: str
    depth: int
    width: int
    difficulty_source: str
    topic: str | None = None  # optional free-form hint from the caller
    params: dict | None = None  # public-seeds: values chosen for the card's template parameters
    card: dict | None = None  # public-seeds: the reverse-engineered card of a public task (generator/public_seeds.py)

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        """Human-readable block used inside prompts."""
        if self.card:
            return self._describe_public()
        lines = [
            f"- Domain: {DOMAINS[self.domain]['label']}",
            f"- Task type: {self.task_type}. {TASK_TYPES[self.task_type]}",
            f"- Stack: {STACKS[self.stack]['label']} (base image `{STACKS[self.stack]['base_image']}`)",
            "- Skills the task must genuinely require:",
            *[f"  - {skill}" for skill in self.skills],
            f"- Persona asking for help: {self.persona}",
            f"- Depth: {self.depth} steps that each depend on the result of the one before",
            f"- Width: {self.width} independent sub-problem(s) that must all be solved",
            f"- Difficulty source: {DIFFICULTY_SOURCES[self.difficulty_source]}",
        ]
        if self.topic:
            lines.append(f"- Topic requested by the user (must be honoured): {self.topic}")
        return "\n".join(lines)


    def _describe_public(self) -> str:
        from generator.public_seeds import card_block  # lazy: public_seeds imports this module

        lines = [
            f"- Domain: {self.card['domain']}",
            f"- Task type: {self.task_type}. {TASK_TYPES[self.task_type]}",
            f"- Stack: {STACKS[self.stack]['label']} (base image `{STACKS[self.stack]['base_image']}`)",
            "- Skills the task must genuinely require:",
            *[f"  - {skill}" for skill in self.skills],
            f"- Persona asking for help: {self.persona}",
            f"- Depth: {self.depth} steps that each depend on the result of the one before",
            f"- Width: {self.width} independent sub-problem(s) that must all be solved",
            card_block(self.card),
        ]
        if self.params:
            lines.append("- Template parameters for this task (use these values): " + ", ".join(f"{k} = {v}" for k, v in self.params.items()))
        if self.topic:
            lines.append(f"- Topic requested by the user (must be honoured): {self.topic}")
        return "\n".join(lines)


def _rng(run_id: str, *parts) -> random.Random:
    digest = hashlib.sha256("|".join([run_id, *map(str, parts)]).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _stratified(values: list[str], run_id: str, label: str, index: int) -> str:
    """Pick so that no value repeats until all have been used.

    The order is reshuffled for each full pass, seeded by the run, so two runs
    do not walk the values in the same order.
    """
    cycle, position = divmod(index, len(values))
    order = sorted(values)
    _rng(run_id, label, cycle).shuffle(order)
    return order[position]


def _domain_and_type(run_id: str, index: int, task_types: tuple[str, ...] = ()) -> tuple[str, str]:
    """Domain and task type for ``index``, balancing task types across the batch.

    Domains are stratified. Each task then takes, from the types its domain
    allows, the one used least by the tasks before it, so a batch does not end
    up dominated by one type just because many domains allow it.

    ``task_types`` limits a run to those types (``--task-types``): only
    domains that allow one of them are drawn, and only those types are used.
    With none given the draw is exactly what it always was.
    """
    wanted = set(task_types)
    domains = [d for d in DOMAINS if not wanted or wanted & set(DOMAINS[d]["task_types"])]
    used = dict.fromkeys(TASK_TYPES, 0)
    domain = task_type = ""
    for i in range(index + 1):
        domain = _stratified(domains, run_id, "domain", i)
        allowed = [t for t in DOMAINS[domain]["task_types"] if not wanted or t in wanted]
        fewest = min(used[t] for t in allowed)
        task_type = _rng(run_id, "type", i).choice(sorted(t for t in allowed if used[t] == fewest))
        used[task_type] += 1
    return domain, task_type


def _public_seed(run_id, index, rng, topic, overrides, task_types) -> "SpecSeed":
    """A seed drawn from a public task's card: its domain, stack and skills come from the card."""
    from generator.public_seeds import draw_card  # lazy: public_seeds imports this module

    from generator.public_seeds import template_parameters

    card = draw_card(run_id, index)
    params = {p["name"]: rng.choice(p["values"]) for p in template_parameters(card)}
    allowed = [t for t in card["task_types"] if not task_types or t in task_types] or list(task_types) or ["implement"]
    seed = SpecSeed(
        index=index,
        domain=card["domain"],
        task_type=rng.choice(allowed),
        stack=card["stack"],
        skills=list(card["skills"]),
        persona=rng.choice(PERSONAS),
        depth=rng.randint(*DEPTH_RANGE),
        width=rng.randint(*WIDTH_RANGE),
        difficulty_source="public-seed",
        topic=topic,
        params=params or None,
        card=card,
    )
    for key, value in (overrides or {}).items():
        setattr(seed, key, value)
    return seed


def sample_seed(
    run_id: str,
    index: int,
    topic: str | None = None,
    overrides: dict | None = None,
    task_types: tuple[str, ...] = (),
) -> SpecSeed:
    """Draw the spec seed for task ``index`` of ``run_id``.

    ``overrides`` may fix any field while keeping the rest of the seed.
    ``task_types`` limits the draw to those task types.
    """
    rng = _rng(run_id, "seed", index)
    if config.design_has("public-seeds"):
        return _public_seed(run_id, index, rng, topic, overrides, task_types)
    # 1-2. Domain (stratified), task type (balanced) and a stack that suits the domain.
    domain, task_type = _domain_and_type(run_id, index, task_types)
    info = DOMAINS[domain]
    stack = rng.choice(info["stacks"])

    # 3. Skills: mostly the domain's own, at most one from a neighbouring domain.
    n_skills = rng.randint(*SKILLS_RANGE)
    borrow = rng.random() < 0.3
    own = rng.sample(info["skills"], n_skills - (1 if borrow else 0))
    skills = list(own)
    if borrow:
        neighbour = rng.choice(info["neighbours"])
        skills.append(rng.choice(DOMAINS[neighbour]["skills"]))

    # 4. Persona and difficulty controls.
    # Drawn in this order from the main stream, whatever the design, so two
    # runs with the same seed key get the same task in every other respect.
    # The staged design takes its depth from its own range and its own stream.
    persona = rng.choice(PERSONAS)
    depth = rng.randint(*DEPTH_RANGE)
    width = rng.randint(*WIDTH_RANGE)
    difficulty_source = rng.choices(
        sorted(DIFFICULTY_SOURCES), weights=[DIFFICULTY_WEIGHTS[s] for s in sorted(DIFFICULTY_SOURCES)]
    )[0]
    seed = SpecSeed(
        index=index,
        domain=domain,
        task_type=task_type,
        stack=stack,
        skills=skills,
        persona=persona,
        depth=depth,
        width=width,
        difficulty_source=difficulty_source,
        topic=topic,
    )
    for key, value in (overrides or {}).items():
        setattr(seed, key, value)
    return seed
