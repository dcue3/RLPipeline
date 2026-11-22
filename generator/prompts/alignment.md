You are checking that a terminal-based software engineering task is fair. An AI coding agent reads the instruction below and can explore every file in the container. Hidden tests then decide whether it succeeded.

A test is fair if what it checks is either stated in the instruction, or discoverable by a careful expert from the files in the container (documentation, source code, data, configuration, version history, or the behaviour of programs already there). A test is unfair only if it expects something the agent could not have known from either.

The instruction the agent sees:
<<<INSTRUCTION
{instruction}
INSTRUCTION>>>

Files that define the container (the Dockerfile and what it copies in; build steps may create more):
{environment}

The hidden tests:
```python
{tests}
```

Reply with a JSON object only, with these keys:

- "tests": one entry for EVERY test function in the file, in order: {{"name": the function name, "checks": one sentence saying what the test asserts, "quote": a sentence or phrase copied EXACTLY from the instruction that states this expectation, or "" if the instruction does not state it, "discoverable_from": the path in the container (for example "/app/docs/FORMAT.md" or "/app/repo/.git") where the agent can find out what is expected, or "" if it is stated in the instruction or cannot be found, "fair": true or false, "inspects_implementation": true or false, "issue": "" or what is wrong}}.
  - "fair" is true if every assertion in the test follows from the instruction, from the named place in the container, or from both together.
  - "fair" is false if the test asserts a detail that neither the instruction nor anything in the container determines (an exact message, an exit code value, an ordering, a file location, a format). Name that detail in "issue".
  - "inspects_implementation" is true if the test reads the agent's source code to check how the solution is written instead of what it does.

- "gives_away_solution": true if the instruction tells the agent what the defects are or how to fix them, leaving little to work out. Otherwise false.

- "problems": a list describing each unfair test. Empty list if every test is fair.
