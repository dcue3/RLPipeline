You are repairing a terminal-based software engineering task that failed an automated check. A valid task must satisfy both of these:
- Oracle: after `solution/solve.sh` runs in a fresh container, every test in `tests/test_outputs.py` passes.
- Nop: in a fresh, untouched container, at least one test fails.

Design specification (the source of truth for what the task should be):
```json
{spec}
```

Current files:
{files}

What failed:
{failure}

Work out which file is actually wrong by reasoning from the specification:
- If the environment does not build or does not match the specification, fix the environment.
- If the solution does not achieve a requirement, fix the solution.
- If a test expects something the specification does not require, has a wrong expected value, or depends on something unavailable in the test runner, fix the test. Never weaken or delete a test just to make it pass, and never make a test pass on the untouched container.
- If the instruction omits or contradicts something a test checks, fix the instruction.

- If the report says a test checks something the instruction does not state, add the missing statement to the instruction (stating the required behaviour, never the fix) or, if the detail is incidental, relax that one assertion. If a test inspects the solution's source code, replace it with a test of behaviour.

- If the instruction is too long, shorten it: keep the symptoms and what a correct result must look like; drop anything the container already documents.
- Never fix a problem by adding a hint to the instruction. The instruction states symptoms and acceptance criteria only: it does not say where documentation or configuration lives, name a defect or its cause, say how many things are wrong, or give advice.

- If the report says a generator script must move: put it under `environment/_build/`, copy it with `COPY _build/ /tmp/_build/`, run it from `/tmp/_build/`, and do not delete it yourself (the pipeline removes `/tmp/_build`). Do not use `COPY . /app`; copy files and directories by name.
- If the report says a file holds the expected result (an expected, reference, golden or answer file): remove it from the environment and from the Dockerfile, and make the instruction state the required behaviour instead of pointing at the file. Tests must work each expected value out themselves.

Reminders: tests run with a separate Python 3.13 that has only the standard library and pytest, as root, from /app. `solve.sh` runs as root from /app with no terminal. Everything must be deterministic.

Reply with only the files that need to change, each complete, in blocks of exactly this form, with nothing before the first block or after the last:

<<<FILE path/relative/to/task>>>
...full new file content...
<<<END>>>
