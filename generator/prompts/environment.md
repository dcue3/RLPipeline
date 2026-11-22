You are building one terminal-based software engineering task from a design specification. In this step you write the container environment and the reference solution. The tests and the instruction are written afterwards from your files.

Design specification:
```json
{spec}
```

Write these files:

1. `environment/Dockerfile`
   - Start with exactly `FROM {base_image}`.
   - Set `WORKDIR /app`. If a build step needs another directory, switch back: the last `WORKDIR` in the file must be `/app`.
   - Install only what the task needs. Pin pip packages (`pip install name==1.2.3`). Do NOT pin apt package versions: use `apt-get update && apt-get install -y --no-install-recommends name ... && rm -rf /var/lib/apt/lists/*`.
   - Use only the language's standard library in Go, Node and C code. Never write a lock file or checksum file by hand (`go.sum`, `package-lock.json`, hash-pinned requirements): you cannot know the hashes, and a wrong one breaks the build.
   - Copy the task files in with `COPY`. The build context is the `environment/` directory.
   - Keep the environment small: about 8 files or fewer besides the Dockerfile. A long reply is cut off and discarded.
   - Build-time generators. If the task needs generated data, version history or any other starting state, the script that creates it goes under `environment/_build/` and nowhere else. Copy that directory with `COPY _build/ /tmp/_build/` and run the script from there (for example `RUN python3 /tmp/_build/make_data.py`), with a fixed seed and fixed dates. The pipeline removes `/tmp/_build` from the image after your last line, so do not delete it yourself, and never copy `_build` anywhere else. In particular do not use `COPY . /app`: copy the task's files and directories by name. A generator shows how the data was made and often what the answer is, so it must never be visible to the solver.
   - The generator must not write any file that contains expected results.
   - No answers in the container. It must not contain the expected result of the task in any form: no expected-output, reference, golden or answer file, and no complete working version of the program the solver is asked to fix or build (not in another file, not in a comment, not in a generator that stays in the image).
   - Never work out by hand a value that the starting state legitimately needs and a program would compute, such as a checksum of an input file or a total stored in a database. Have a build step under `_build/` compute it. This does not extend to the task's expected result: that is never computed, stored or shipped in the container at all.
   - Do not copy or mention the solution or the tests. Do not set an ENTRYPOINT or CMD.

2. Every other file the container needs, each under `environment/` (for example `environment/src/parser.py`). These are the starting files described in the specification: the broken, incomplete or legacy state the agent begins from. They must be complete, realistic and runnable, and must not contain comments that give away the solution or point at the defects. Write the misleading material the specification calls for exactly as a real project would have it: plausible, unremarkable, and not flagged as wrong or out of date. The truth must be in what the data or the programs actually do, so that it is found by checking, not by reading.

3. `solution/solve.sh`
   - A bash script starting with `#!/bin/bash` and `set -euo pipefail`.
   - Runs inside the container as root from /app, with no input from a terminal. It must not depend on network access: everything it needs is already in the image.
   - Takes the starting state to a state that satisfies every requirement in the specification.
   - When it changes a file, it rewrites the whole file with a quoted heredoc (`cat > path <<'EOF'`). Do not patch with sed, awk or ed, which is fragile.
   - It must do the real work. It must not simply write the expected output files when the task is to fix or build a program that produces them.

Rules for everything you write:
- Fully deterministic. No dependence on the current date or time, random values without a fixed seed, network content or timing.
- The starting state must actually exhibit every pitfall in the specification, and the reference solution must handle each one.
- Text files only. Anything binary must be generated at build time.
- Think through the solution carefully and check it against every requirement before answering. The solution will be executed, and the task is discarded if it does not work.

Output format. Reply with the files only, each in a block of exactly this form, with nothing before the first block or after the last:

<<<FILE environment/Dockerfile>>>
...file content...
<<<END>>>
