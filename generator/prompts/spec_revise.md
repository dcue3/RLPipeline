You are revising the design specification of a terminal-based software engineering task. A reviewer found defects. Produce a corrected specification that fixes every one of them while keeping the same scenario, the same difficulty and the same sampled constraints.

Sampled constraints the task must honour:
{seed}

Current specification:
```json
{spec}
```

Defects to fix:
{problems}

Rules for the corrected specification:
- Every requirement states WHAT must be observably true, never HOW to implement it. Do not name commands, options, functions or algorithms the solver should use internally. Requirements may still fix interfaces a user or test must call or read: paths, command names, target names, formats, field names, exit codes.
- The "scenario" and "requirements" are shown to the solver; "pitfalls", "solution_outline" and "environment" are private design notes. Keep every defect and its cause in the private notes only.
- If two requirements contradict each other, change one so both can hold; do not simply delete one.
- The scenario describes the situation and the symptoms from the persona's point of view. It does not list the defects or their fixes; the solver must diagnose them.
- Every requirement can be checked by a program observing the final container state, with the same result on every run. Nothing may depend on timing, races or signal ordering.
- Every pitfall remains knowable to a careful expert from the container. Each "knowable_from" names a file, a command's output or an observable behaviour in the container, never the instruction.
- Keep within the size limits: between {req_low} and {req_high} requirements and between {pit_low} and {pit_high} pitfalls. A specification outside them is discarded. Do not add requirements: fix the ones that are there, and merge or reword instead of splitting.
- Do not make the task easier. If a requirement was giving the answer away, restate it as the behaviour expected; do not delete the difficulty.

Reply with the complete corrected specification as a JSON object only, with exactly the same keys as the current one.
