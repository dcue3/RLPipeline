You are finishing one terminal-based software engineering task. The container environment and the reference solution exist. In this step you write the hidden tests and the instruction the solving agent will read.

Design specification:
```json
{spec}
```

Files in the task so far:
{files}

Write these two files.

1. `tests/test_outputs.py` - pytest tests that decide whether the task was solved.
   How they run: after the agent has finished, in the same container, as root, with /app as the working directory, using a separate Python 3.13 that has only the standard library and pytest. The tests therefore cannot import packages installed in the task image; to exercise the agent's work they run its programs with `subprocess` (always with a `timeout`) and read the files it produced.
   Programs the tests launch (`python3`, `pip`, `node`, `make` ...) resolve to the task image's own, the same ones the agent used.
   The reference solution is NOT in the container when an agent's work is graded. You are shown `solve.sh` only so you understand the intended end state. Tests must never run it, read it or refer to it.
   Rules:
   - Cover every requirement in the specification, each with a test of the outcome it describes.
   - Check behaviour, not appearance. Run the program on inputs and compare results. Do not test that a file merely exists, that source code contains a particular string, or how the solution is written.
   - Include the fresh-input checks from the specification: build new inputs inside the test (in a temporary directory), run the agent's program on them and compare with expected values worked out in the test itself or written out by hand. A solution that hard-codes the outputs for the shipped files must fail.
   - Do not compare against an expected-output file in the container; there is none. Work each expected value out in the test.
   - Every expected value must be right. Derive each one carefully from the specification; do not guess. Never compute expected values by calling the code under test.
   - Everything a test checks must be stated in the instruction or discoverable in the container (documentation, existing files, data, or the behaviour of programs already there). Do not assert incidental details such as exact log wording, whitespace or ordering unless the instruction or the container fixes them.
   - Every test must fail or error on the untouched starting container where the requirement is not yet met, and all tests must pass once `solution/solve.sh` has run.
   - Tests must be deterministic and must not modify the agent's deliverables in a way that affects later tests. Do not write tests whose result depends on timing, sleeps, races or signal delivery.
   - Never read the agent's source code to look for particular commands or constructs. Only observable behaviour counts.
   - Write 5 to 10 tests that check outcomes: run the work end to end and compare what it produces. Prefer a few meaningful checks to many small ones. Every test function is named `test_...` and has a one-line docstring saying what it checks.

2. `instruction.md` - what the solving agent is told.
   - Write it as the persona in the specification would. It contains two things only: the symptoms (what the persona observes going wrong or missing) and the acceptance criteria (what a correct result must look like: the paths, formats, commands and exit codes the tests rely on).
   - At most about 250 words. No requirement ids, no numbered specification, no headings.
   - It states the required behaviour. It must NOT tell the solver to match, reproduce or compare against a file in the container: no such file exists, and the tests check the behaviour on the shipped data and on fresh inputs.
   - It must NOT:
     - say where documentation, configuration or any other source of the answer lives ("the format is documented in ...", "the policy lives in ..."). The solver finds that by exploring.
     - name any defect or its cause, or say how many things are wrong.
     - give advice or hints of any kind ("one thing I've noticed", "be careful with", "make sure to check", "you may want to", "I suspect").
     - explain how to solve the task, or mention the tests, the solution or this specification.
   - Everything a test checks must still be stated in the instruction or discoverable in the container. Rely on the container for the detail: the solver is expected to investigate it.
   - Plain Markdown, no title heading needed.

Output format. Reply with the two files only, each in a block of exactly this form, with nothing before the first block or after the last:

<<<FILE tests/test_outputs.py>>>
...file content...
<<<END>>>
